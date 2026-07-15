import discord
from discord.ext import commands
import google.generativeai as genai
import json
import re
import config
from database.db_manager import db

# 初始化 Gemini
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')

class AIActionConfirmView(discord.ui.View):
    def __init__(self, action_data: dict, guild_id: int):
        super().__init__(timeout=180)
        self.action_data = action_data
        self.guild_id = guild_id

    @discord.ui.button(label="✅ 確認執行", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 根據 AI 判斷的 action 執行對應的資料庫操作
        if self.action_data['action'] == 'expense':
            title = self.action_data['title']
            amount = self.action_data['amount']
            payer_id = self.action_data['payer_id']
            
            # 獲取室友名單來分攤
            rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", self.guild_id)
            participating_rms = [r['user_id'] for r in rms]
            
            if not participating_rms:
                return await interaction.followup.send("❌ 找不到室友名單。")
                
            split = amount // len(participating_rms)
            
            async with db.transaction() as conn:
                eid = await conn.fetchval(
                    "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                    self.guild_id, title, amount, interaction.user.id
                )
                for uid in participating_rms:
                    if uid != payer_id:
                        await conn.execute(
                            "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                            self.guild_id, eid, uid, payer_id, split
                        )
            await interaction.followup.send(f"🎉 已成功記帳：{title} (${amount})！")
            
        # 執行完畢後禁用按鈕
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="❌ 取消", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="🚫 已取消該操作。", view=self)

class AIAgentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 排除機器人自己的訊息與非 AI 頻道的訊息
        if message.author.bot or message.channel.id != config.AI_CHANNEL_ID:
            return
        if not config.GEMINI_API_KEY:
            return await message.channel.send("⚠️ 尚未設定 Gemini API Key。")

        # 獲取當前伺服器的室友名單，注入給 AI 防止認錯人
        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", message.guild.id)
        roommate_info = "\n".join([f"姓名: {message.guild.get_member(r['user_id']).display_name}, ID: {r['user_id']}" for r in rms if message.guild.get_member(r['user_id'])])

        system_prompt = f"""
        你是一個精準的 Discord 室友生活助理。你的任務是判斷用戶的輸入是否需要執行「記帳指令」。
        【目前群組內的室友名單與真實 ID】：
        {roommate_info}
        
        【重要規則】：
        1. 如果用戶的話語中包含幫大家付錢、墊錢、買了什麼東西要分攤的意思，請「嚴格」回覆一個 JSON 格式，絕對不要包含任何其他文字或 markdown 符號。
        2. JSON 格式為：{{"action": "expense", "title": "物品名稱", "amount": 總金額整數, "payer_id": 墊款人的真實ID}}
        3. 如果判斷 payer_id 是用戶自己，請使用 ID: {message.author.id}。
        4. 若只是普通的閒聊或問題，請直接用自然的繁體中文回覆他，不要輸出 JSON。
        """

        async with message.channel.typing():
            try:
                response = model.generate_content(
                    f"{system_prompt}\n\n用戶說：{message.content}",
                    generation_config=genai.types.GenerationConfig(temperature=0.1) # 溫度設極低，降低幻覺
                )
                reply = response.text.strip()
                
                # 嘗試解析是否為嚴格的 JSON 動作指令
                # 簡單正則處理，去掉可能被加上去的 ```json ``` 標籤
                json_str = re.sub(r'```json\n|\n```|```', '', reply).strip()
                
                try:
                    action_data = json.loads(json_str)
                    if action_data.get("action") == "expense":
                        payer_mention = f"<@{action_data['payer_id']}>"
                        embed = discord.Embed(title="🤖 AI 偵測到記帳指令", description="請問是否要執行以下記帳？", color=discord.Color.gold())
                        embed.add_field(name="項目", value=action_data['title'], inline=True)
                        embed.add_field(name="金額", value=f"${action_data['amount']}", inline=True)
                        embed.add_field(name="墊款人", value=payer_mention, inline=False)
                        
                        view = AIActionConfirmView(action_data, message.guild.id)
                        await message.reply(embed=embed, view=view)
                        return
                except json.JSONDecodeError:
                    # 如果不是 JSON，代表是普通聊天，直接回覆
                    await message.reply(reply)

            except Exception as e:
                await message.reply(f"❌ AI 腦袋卡住了：{e}")

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

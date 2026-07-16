import discord
from discord.ext import commands
import google.generativeai as genai
import json
import re
import config
from database.db_manager import db
import logging

logger = logging.getLogger("bot.ai")

# 初始化 Gemini
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)

class AIActionConfirmView(discord.ui.View):
    def __init__(self, action_data: dict, guild_id: int):
        super().__init__(timeout=180)
        self.action_data = action_data
        self.guild_id = guild_id

    @discord.ui.button(label="✅ 確認執行", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        try:
            if self.action_data['action'] == 'expense':
                title = self.action_data['title']
                amount = self.action_data['amount']
                
                # 🛡️ 防呆機制：如果 AI 沒給 ID，預設為點擊按鈕的使用者
                payer_id = self.action_data.get('payer_id')
                if not payer_id or payer_id == "None":
                    payer_id = interaction.user.id
                
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
                
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            
        except Exception as e:
            # 🛡️ 如果發生錯誤，回報在頻道上而不是讓按鈕卡死
            await interaction.followup.send(f"❌ 寫入資料庫失敗：{e}")

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
        if message.author.bot or message.channel.id != config.AI_CHANNEL_ID:
            return
        if not config.GEMINI_API_KEY:
            return await message.channel.send("⚠️ AI 尚未準備就緒或未設定金鑰。")

        # 獲取室友名單
        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", message.guild.id)
        roommate_info = "\n".join([f"姓名: {message.guild.get_member(r['user_id']).display_name}, ID: {r['user_id']}" for r in rms if message.guild.get_member(r['user_id'])])

        # 系統核心指令
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

        # 依照你指定的優先度排序模型備用清單
        fallback_models = [
            'gemini-3.5-flash',
            'gemini-3-flash',
            'gemini-2.5-flash',
            'gemini-3.1-flash-lite',
            'gemini-2.5-flash-lite'
        ]

        async with message.channel.typing():
            reply = None
            for target_model in fallback_models:
                try:
                    # 動態注入系統指令，隔離對話與規則
                    temp_model = genai.GenerativeModel(
                        model_name=target_model,
                        system_instruction=system_prompt
                    )
                    
                    # 僅將用戶純淨的話語送入模型
                    response = temp_model.generate_content(
                        message.content,
                        generation_config=genai.types.GenerationConfig(temperature=0.1)
                    )
                    reply = response.text.strip()
                    logger.info(f"✅ 成功使用模型: {target_model} 完成任務")
                    break  # 成功生成，立刻跳出迴圈
                    
                except Exception as e:
                    logger.warning(f"⚠️ {target_model} 失敗或額度用盡，自動切換下一個模型... ({e})")
                    continue
                    
            if not reply:
                return await message.reply("❌ 糟糕，所有 AI 模型的免費額度都已耗盡或發生錯誤，請稍後再試！")
                
            # 嘗試解析 JSON
            json_str = re.sub(r'```json\n|\n```|```', '', reply).strip()
            
            try:
                action_data = json.loads(json_str)
                if action_data.get("action") == "expense":
                    
                    # 🛡️ 雙重防呆：確保介面上顯示的墊款人也不會是 <@None>
                    payer_id = action_data.get('payer_id')
                    if not payer_id or payer_id == "None":
                        payer_id = message.author.id
                        action_data['payer_id'] = payer_id  # 同步更新丟給確認按鈕的資料
                        
                    payer_mention = f"<@{payer_id}>"
                    
                    embed = discord.Embed(title="🤖 AI 偵測到記帳指令", description="請問是否要執行以下記帳？", color=discord.Color.gold())
                    embed.add_field(name="項目", value=action_data['title'], inline=True)
                    embed.add_field(name="金額", value=f"${action_data['amount']}", inline=True)
                    embed.add_field(name="墊款人", value=payer_mention, inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
            except json.JSONDecodeError:
                # 不是 JSON，視為普通聊天
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

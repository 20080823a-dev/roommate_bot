import discord
from discord.ext import commands
import google.generativeai as genai
import json
import re
import config
from database.db_manager import db
import logging

logger = logging.getLogger("bot.ai")

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
            # 💡 【情況 1：一般墊款均分】
            if self.action_data['action'] == 'expense':
                title = self.action_data['title']
                amount = self.action_data['amount']
                
                payer_id = self.action_data.get('payer_id')
                if not payer_id or str(payer_id).lower() == "none":
                    payer_id = interaction.user.id
                else:
                    payer_id = int(payer_id)
                
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
            
            # 💡 【情況 2：A 還款給 B】
            elif self.action_data['action'] == 'repay':
                amount = self.action_data['amount']
                payer_id = self.action_data.get('payer_id')
                receiver_id = self.action_data.get('receiver_id')

                # 防呆強制轉整數
                try: payer_id = int(payer_id)
                except: payer_id = interaction.user.id
                try: receiver_id = int(receiver_id)
                except: receiver_id = interaction.user.id

                async with db.transaction() as conn:
                    eid = await conn.fetchval(
                        "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                        self.guild_id, "還款/結清", amount, interaction.user.id
                    )
                    # 還款邏輯：收錢的人 (receiver) 欠 付錢的人 (payer)，以此抵銷原本的債務
                    await conn.execute(
                        "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                        self.guild_id, eid, receiver_id, payer_id, amount
                    )
                await interaction.followup.send(f"💸 已成功記錄還款：<@{payer_id}> 還給 <@{receiver_id}> ${amount}！")
                
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            
        except Exception as e:
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

        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", message.guild.id)
        roommate_info = "\n".join([f"姓名: {message.guild.get_member(r['user_id']).display_name}, ID: {r['user_id']}" for r in rms if message.guild.get_member(r['user_id'])])

        # 💡 升級版系統指令：教會 AI 辨識 @標記 與「還款」邏輯
        system_prompt = f"""
        你是一個精準的 Discord 室友生活助理。你的任務是判斷用戶的輸入是否需要執行「記帳指令」或「還款指令」。
        【目前群組內的室友名單與真實 ID】：
        {roommate_info}
        發話者 (用戶自己) 的真實 ID 是: {message.author.id}
        
        【重要規則】：
        1. 當訊息中出現 `<@數字>` 的格式，這代表用戶標記了某人，其中的「數字」就是真實 ID，請直接提取數字使用。
        2. 【一般墊款均分】：如果用戶說幫大家付錢、墊錢、買東西要分攤，回傳 JSON：
           {{"action": "expense", "title": "物品名稱", "amount": 總金額整數, "payer_id": 墊款人的真實ID}}
        3. 【還款 / 給錢】：如果是 A 還錢給 B、A 給 B 多少錢 (例如「@軒 還我 500」)，回傳 JSON：
           {{"action": "repay", "amount": 總金額整數, "payer_id": 付錢方(還款人)的真實ID, "receiver_id": 收錢方的真實ID}}
           * 注意：如果用戶說「我」或「還我」，代表發話者 ({message.author.id})。
        4. 嚴格輸出純 JSON，不可有其他文字與 markdown 符號。若是閒聊則自然回覆中文，不要輸出 JSON。
        """

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
                    temp_model = genai.GenerativeModel(
                        model_name=target_model,
                        system_instruction=system_prompt
                    )
                    
                    response = temp_model.generate_content(
                        message.content,
                        generation_config=genai.types.GenerationConfig(temperature=0.1)
                    )
                    reply = response.text.strip()
                    break
                    
                except Exception as e:
                    logger.warning(f"⚠️ {target_model} 失敗: {e}")
                    continue
                    
            if not reply:
                return await message.reply("❌ AI 發生錯誤，請稍後再試！")
                
            json_str = re.sub(r'```json\n|\n```|```', '', reply).strip()
            
            try:
                action_data = json.loads(json_str)
                
                # 介面顯示 1：一般均分
                if action_data.get("action") == "expense":
                    payer_id = action_data.get('payer_id')
                    if not payer_id or str(payer_id).lower() == "none":
                        payer_id = message.author.id
                        action_data['payer_id'] = payer_id
                        
                    embed = discord.Embed(title="🤖 AI 偵測到均分記帳", description="請問是否要執行以下記帳？", color=discord.Color.gold())
                    embed.add_field(name="項目", value=action_data['title'], inline=True)
                    embed.add_field(name="金額", value=f"${action_data['amount']}", inline=True)
                    embed.add_field(name="墊款人", value=f"<@{action_data['payer_id']}>", inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
                
                # 介面顯示 2：還款結清
                elif action_data.get("action") == "repay":
                    payer_id = action_data.get('payer_id')
                    receiver_id = action_data.get('receiver_id')
                    
                    if not payer_id or str(payer_id).lower() == "none": payer_id = message.author.id
                    if not receiver_id or str(receiver_id).lower() == "none": receiver_id = message.author.id
                    
                    action_data['payer_id'] = payer_id
                    action_data['receiver_id'] = receiver_id
                    
                    embed = discord.Embed(title="💸 AI 偵測到還款指令", description="請問是否要執行以下還款紀錄？", color=discord.Color.green())
                    embed.add_field(name="金額", value=f"${action_data['amount']}", inline=False)
                    embed.add_field(name="還款人 (付錢方)", value=f"<@{action_data['payer_id']}>", inline=True)
                    embed.add_field(name="收款人 (收錢方)", value=f"<@{action_data['receiver_id']}>", inline=True)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
                    
            except json.JSONDecodeError:
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

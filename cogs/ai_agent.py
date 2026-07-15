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
        
        if self.action_data['action'] == 'expense':
            title = self.action_data['title']
            amount = self.action_data['amount']
            payer_id = self.action_data['payer_id']
            
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
            return await message.channel.send("⚠️ AI 尚未設定金鑰。")

        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", message.guild.id)
        roommate_info = "\n".join([f"姓名: {message.guild.get_member(r['user_id']).display_name}, ID: {r['user_id']}" for r in rms if message.guild.get_member(r['user_id'])])

        system_prompt = f"""
        你是一個精準的 Discord 室友生活助理。你的任務是判斷用戶的輸入是否需要執行「記帳指令」。
        【目前群組內的室友名單與真實 ID】：
        {roommate_info}
        
        【重要規則】：
        1. 若用戶想記帳，請嚴格輸出 JSON：{{"action": "expense", "title": "名稱", "amount": 金額, "payer_id": ID}}
        2. 若只是閒聊，請用自然的繁體中文回覆。
        """

        # 設定你的降級模型優先順序 (Fallback List)
        fallback_models = [
            'gemini-3.5-flash', 
            'gemini-3.0-flash', 
            'gemini-2.5-flash', 
            'gemini-3.1-flash-lite', 
            'gemini-2.5-flash-lite'
        ]
        
        async with message.channel.typing():
            reply = None
            for target_model in fallback_models:
                try:
                    # 使用 system_instruction 隔離對話與規則
                    model = genai.GenerativeModel(
                        model_name=target_model,
                        system_instruction=system_prompt
                    )
                    
                    response = model.generate_content(
                        message.content,
                        generation_config=genai.types.GenerationConfig(temperature=0.1)
                    )
                    reply = response.text.strip()
                    logger.info(f"✅ 使用模型 {target_model} 處理成功")
                    break
                except Exception as e:
                    logger.warning(f"⚠️ {target_model} 處理失敗，嘗試下一個: {e}")
                    continue

            if not reply:
                return await message.reply("❌ AI 資源忙碌中，請稍候重試。")

            json_str = re.sub(r'```json\n|\n```|```', '', reply).strip()
            try:
                action_data = json.loads(json_str)
                if action_data.get("action") == "expense":
                    embed = discord.Embed(title="🤖 AI 偵測到記帳指令", color=discord.Color.gold())
                    embed.add_field(name="項目", value=action_data['title'], inline=True)
                    embed.add_field(name="金額", value=f"${action_data['amount']}", inline=True)
                    embed.add_field(name="墊款人", value=f"<@{action_data['payer_id']}>", inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
            except:
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

import discord
from discord.ext import commands
import google.generativeai as genai
import json
import re
import config
from database.db_manager import db
import logging

logger = logging.getLogger("bot.ai")

# --- 自動抓取最新模型的動態初始化 ---
model = None
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)
    
    try:
        # 1. 向 API 請求當前所有可用模型的清單
        available_models = []
        for m in genai.list_models():
            model_name = m.name.replace('models/', '')
            # 2. 嚴格篩選：只抓取支援 generateContent，且名稱完全符合「gemini-數字.數字-flash」格式的模型
            if 'generateContent' in m.supported_generation_methods and re.match(r'^gemini-\d+\.\d+-flash$', model_name):
                available_models.append(model_name)
        
        if available_models:
            # 3. 依字母與數字降序排列
            available_models.sort(reverse=True)
            latest_model_name = available_models[0]
            logger.info(f"🤖 成功動態加載最新 AI 穩定版模型: {latest_model_name}")
            model = genai.GenerativeModel(latest_model_name)
        else:
            logger.warning("⚠️ 找不到符合標準版 flash 的模型，退回使用預設值。")
            model = genai.GenerativeModel('gemini-2.5-flash')
            
    except Exception as e:
        logger.error(f"❌ 動態獲取模型清單失敗: {e}，改用預設模型。")
        model = genai.GenerativeModel('gemini-2.5-flash')

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
        if not config.GEMINI_API_KEY or not model:
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

        # 建立你的「模型備用清單」(按聰明程度/優先度排序)
        fallback_models = ['gemini-3.5-flash', 'gemini-3-flash', 'gemini-2.5-flash', 'gemini-3.1-flash-lite', 'gemini-2.5-flash-lite']
        
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
                    logger.info(f"✅ 成功使用模型: {target_model} 完成任務")
                    break  # 成功生成，立刻跳出迴圈！
                    
                except Exception as e:
                    logger.warning(f"⚠️ {target_model} 失敗或額度用盡，自動切換下一個模型... ({e})")
                    continue  # 遇到錯誤，無縫接軌換下一個模型嘗試
                    
            # 如果迴圈跑完，reply 還是 None，代表所有模型都死光了
            if not reply:
                return await message.reply("❌ 糟糕，所有 AI 模型的免費額度都已耗盡，請稍後再試！")

            except Exception as e:
                await message.reply(f"❌ AI 腦袋卡住了：{e}")

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

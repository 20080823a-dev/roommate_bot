# cogs/ai_agent.py
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
            if self.action_data['action'] == 'expense':
                title = self.action_data['title']
                amount = int(self.action_data.get('amount', 0))
                
                if amount <= 0:
                    return await interaction.followup.send("❌ 記帳失敗：金額必須大於 0！")
                
                payer_id = self.action_data.get('payer_id')
                if not payer_id or str(payer_id).lower() == "none":
                    payer_id = interaction.user.id
                else:
                    payer_id = int(payer_id)
                
                raw_participants = self.action_data.get('participants', [])
                if not raw_participants:
                    rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", self.guild_id)
                    participating_rms = [r['user_id'] for r in rms]
                else:
                    participating_rms = []
                    for uid in raw_participants:
                        try: participating_rms.append(int(uid))
                        except: pass
                    participating_rms = list(set(participating_rms)) 
                
                if not participating_rms:
                    return await interaction.followup.send("❌ 記帳失敗：找不到可以分攤的室友名單。")
                if len(participating_rms) == 1 and participating_rms[0] == payer_id:
                    return await interaction.followup.send("❌ 記帳失敗：不能只有代墊人自己一個人分攤！")
                    
                split = amount // len(participating_rms)
                
                try:
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
                    await interaction.followup.send(f"🎉 已成功記帳：{title} (${amount})，共 {len(participating_rms)} 人平分！")
                except Exception as db_err:
                    return await interaction.followup.send(f"❌ 資料庫寫入失敗：{db_err}")
            
            elif self.action_data['action'] == 'repay':
                amount = int(self.action_data.get('amount', 0))
                if amount <= 0:
                    return await interaction.followup.send("❌ 還款失敗：金額必須大於 0！")
                    
                payer_id = self.action_data.get('payer_id')
                receiver_id = self.action_data.get('receiver_id')

                try: payer_id = int(payer_id)
                except: payer_id = interaction.user.id
                try: receiver_id = int(receiver_id)
                except: receiver_id = interaction.user.id

                try:
                    async with db.transaction() as conn:
                        eid = await conn.fetchval(
                            "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                            self.guild_id, "還款/結清", amount, interaction.user.id
                        )
                        await conn.execute(
                            "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                            self.guild_id, eid, receiver_id, payer_id, amount
                        )
                    await interaction.followup.send(f"💸 已成功記錄還款：<@{payer_id}> 還給 <@{receiver_id}> ${amount}！")
                except Exception as db_err:
                    return await interaction.followup.send(f"❌ 資料庫還款寫入失敗：{db_err}")

            elif self.action_data['action'] == 'shopping':
                # 配合原始資料庫 shopping_items 提取名稱與數量
                item_name = self.action_data.get('item_name', '未知物品')
                quantity = self.action_data.get('quantity', '')
                try:
                    async with db.transaction() as conn:
                        await conn.execute(
                            """
                            INSERT INTO shopping_items (guild_id, item_name, quantity, added_by) 
                            VALUES ($1, $2, $3, $4)
                            """,
                            self.guild_id, item_name, quantity, interaction.user.id
                        )
                    qty_text = f" ({quantity})" if quantity else ""
                    await interaction.followup.send(f"🛒 已成功將 **{item_name}**{qty_text} 加入採購清單！\n*(可使用 `/buy list` 領取任務)*")
                except Exception as db_err:
                    await interaction.followup.send(f"❌ 採購清單寫入失敗：{db_err}")
                
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            
        except Exception as e:
            await interaction.followup.send(f"❌ 發生未預期的系統錯誤：{e}")

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
        roommate_info = "\n".join([f"姓名: {message.guild.get_member(r['user_id']).display_name if message.guild.get_member(r['user_id']) else r['user_id']}, ID: {r['user_id']}" for r in rms])

        # 💡 更新提示詞：要求 AI 將「物品名稱」與「數量」分離
        system_prompt = f"""
        你是一個精準的 Discord 室友生活助理。你的任務是判斷用戶的輸入是否需要執行「記帳指令」、「還款指令」或「採購指令」。
        【目前群組內的室友名單與真實 ID】：
        {roommate_info}
        發話者 (用戶自己) 的真實 ID 是: {message.author.id}
        
        【重要規則】：
        1. 當訊息中出現 `<@數字>` 的格式，這代表用戶標記了某人，其中的「數字」就是真實 ID，請直接提取數字使用。
        2. 【墊款均分】：如果用戶說買東西要分攤，回傳 JSON：
           {{"action": "expense", "title": "物品名稱", "amount": 總金額整數, "payer_id": 墊款人的真實ID, "participants": []}}
           * ⚠️ 如果用戶「有特別標記某幾個人」平分，請將他們的真實 ID 放入 participants 陣列中 (如 [123, 456])。
           * ⚠️ 如果用戶說「幫大家/所有人」墊錢，或「沒有」特別標記誰，請將 participants 保持為空陣列 []。
        3. 【還款 / 給錢】：如果是 A 還錢給 B (例如「@軒 還我 500」)，回傳 JSON：
           {{"action": "repay", "amount": 總金額整數, "payer_id": 付錢方(還款人)的真實ID, "receiver_id": 收錢方的真實ID}}
        4. 【採購清單】：如果用戶想要買東西或將物品加入採買清單 (例如「我要買衛生紙5串」)，回傳 JSON：
           {{"action": "shopping", "item_name": "物品名稱(如:衛生紙)", "quantity": "數量(如:5串，若無則填空字串)"}}
        5. 嚴格輸出純 JSON，不可有其他文字與 markdown 符號。若是閒聊則自然回覆中文，不要輸出 JSON。
        """

        fallback_models = [
            'gemini-3.1-flash-lite',
            'gemini-3.5-flash',
            'gemini-2.5-flash',
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
                return await message.reply("❌ AI 發生錯誤，所有備用模型均已達到限制，請稍後再試！")
                
            json_str = re.sub(r'```json\n|\n```|```', '', reply).strip()
            
            try:
                action_data = json.loads(json_str)
                
                if action_data.get("action") == "expense":
                    payer_id = action_data.get('payer_id')
                    if not payer_id or str(payer_id).lower() == "none":
                        payer_id = message.author.id
                        action_data['payer_id'] = payer_id
                        
                    embed = discord.Embed(title="🤖 AI 偵測到記帳需求", description="請問是否要執行以下記帳？", color=discord.Color.gold())
                    embed.add_field(name="項目", value=action_data['title'], inline=True)
                    embed.add_field(name="金額", value=f"${action_data['amount']}", inline=True)
                    embed.add_field(name="墊款人", value=f"<@{action_data['payer_id']}>", inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
                
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

                elif action_data.get("action") == "shopping":
                    # 顯示獨立分開的物品名稱與數量
                    item_name = action_data.get('item_name', '未知物品')
                    quantity = action_data.get('quantity', '')
                    qty_text = f" ({quantity})" if quantity else ""
                    
                    embed = discord.Embed(title="🛒 AI 偵測到採購需求", description="請問是否加入採購清單？", color=discord.Color.blue())
                    embed.add_field(name="採買項目", value=f"{item_name}{qty_text}", inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
                    
            except json.JSONDecodeError:
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

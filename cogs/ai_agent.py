# cogs/ai_agent.py
import discord
from discord.ext import commands
import google.generativeai as genai
import json
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
                title = self.action_data.get('title', '未命名項目')
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
                        
                        insert_data = [
                            (self.guild_id, eid, uid, payer_id, split)
                            for uid in participating_rms if uid != payer_id
                        ]
                        if insert_data:
                            await conn.executemany(
                                """
                                INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) 
                                VALUES ($1, $2, $3, $4, $5)
                                """,
                                insert_data
                            )
                            
                    await interaction.followup.send(f"🎉 已成功記帳：{title} (${amount})，共 {len(participating_rms)} 人平分！")
                except Exception as db_err:
                    logger.error(f"記帳寫入失敗: {db_err}", exc_info=db_err)
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
                    logger.error(f"還款寫入失敗: {db_err}", exc_info=db_err)
                    return await interaction.followup.send(f"❌ 資料庫還款寫入失敗：{db_err}")

            elif self.action_data['action'] == 'shopping':
                item_name = self.action_data.get('item_name', '未知物品')
                quantity = self.action_data.get('quantity', '')
                try:
                    async with db.transaction() as conn:
                        item_id = await conn.fetchval(
                            """
                            INSERT INTO shopping_items (guild_id, item_name, quantity, added_by) 
                            VALUES ($1, $2, $3, $4) RETURNING id
                            """,
                            self.guild_id, item_name, quantity, interaction.user.id
                        )
                        
                    qty_text = f" ({quantity})" if quantity else ""
                    
                    from cogs.shopping import PurchaseButton
                    view = discord.ui.View(timeout=None)
                    view.add_item(PurchaseButton(str(item_id), item_name))
                    
                    await interaction.followup.send(f"🛒 已成功將 **{item_name}**{qty_text} 加入採購清單！\n*(點擊下方按鈕即可領取購買任務)*", view=view)
                except Exception as db_err:
                    logger.error(f"採購清單寫入失敗: {db_err}", exc_info=db_err)
                    await interaction.followup.send(f"❌ 採購清單寫入失敗：{db_err}")
                
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            
        except Exception as e:
            logger.error(f"系統錯誤: {e}", exc_info=e)
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

        system_prompt = f"""
        你是一個精準的 Discord 室友生活助理。你的任務是判斷用戶的輸入是否需要執行「記帳指令」、「還款指令」、「採購指令」或是「純粹閒聊」。
        
        <roommate_list>
        {roommate_info}
        </roommate_list>
        
        <context>
        發話者 (用戶自己) 的真實 ID 是: {message.author.id}
        </context>
        
        【重要規則】：
        1. 當訊息中出現 `<@數字>` 的格式，請直接提取其中的數字作為真實 ID 使用。
        2. 【墊款均分】：如果用戶說買東西要分攤，action 設為 "expense" 並填寫 title, amount, payer_id。若指定特定對象，將他們放入 participants；若無指定或幫所有人墊，保持空陣列 []。
        3. 【還款 / 給錢】：如果是 A 還錢給 B，action 設為 "repay" 並填寫 amount, payer_id, receiver_id。
        4. 【採購清單】：想將物品加入採買清單，action 設為 "shopping" 並填寫 item_name, quantity。
        5. 【閒聊與對話】：若是上述功能以外的對話，action 請設為 "chat"，並將你的回應文字放在 reply 欄位。
        """

        # 宣告強型別的 JSON Schema，防止模型隨意輸出文字型金額或欄位
        response_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["expense", "repay", "shopping", "chat"]},
                "title": {"type": "string", "description": "記帳項目名稱"},
                "amount": {"type": "integer", "description": "總金額(只能是整數)"},
                "payer_id": {"type": "integer", "description": "墊款人或還款人的 ID"},
                "participants": {"type": "array", "items": {"type": "integer"}, "description": "參與平分的人"},
                "receiver_id": {"type": "integer", "description": "收錢方的 ID"},
                "item_name": {"type": "string", "description": "採購的物品名稱"},
                "quantity": {"type": "string", "description": "採購的數量"},
                "reply": {"type": "string", "description": "閒聊時的回應文字"}
            },
            "required": ["action"]
        }

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
                        generation_config=genai.types.GenerationConfig(
                            temperature=0.1,
                            response_mime_type="application/json",
                            response_schema=response_schema
                        )
                    )
                    reply = response.text.strip()
                    break
                except Exception as e:
                    logger.warning(f"⚠️ {target_model} 失敗: {e}")
                    continue
                    
            if not reply:
                return await message.reply("❌ AI 發生錯誤，所有備用模型均已達到限制，請稍後再試！")
            
            try:
                action_data = json.loads(reply)
                
                # 攔截並直接回應閒聊模式
                if action_data.get("action") == "chat":
                    reply_text = action_data.get("reply", "我聽不懂你的意思，可以再說清楚一點嗎？")
                    return await message.reply(reply_text)
                
                if action_data.get("action") == "expense":
                    payer_id = action_data.get('payer_id')
                    if not payer_id or str(payer_id).lower() == "none":
                        payer_id = message.author.id
                        action_data['payer_id'] = payer_id
                        
                    embed = discord.Embed(title="🤖 AI 偵測到記帳需求", description="請問是否要執行以下記帳？", color=discord.Color.gold())
                    embed.add_field(name="項目", value=action_data.get('title', '未知'), inline=True)
                    embed.add_field(name="金額", value=f"${action_data.get('amount', 0)}", inline=True)
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
                    embed.add_field(name="金額", value=f"${action_data.get('amount', 0)}", inline=False)
                    embed.add_field(name="還款人 (付錢方)", value=f"<@{action_data['payer_id']}>", inline=True)
                    embed.add_field(name="收款人 (收錢方)", value=f"<@{action_data['receiver_id']}>", inline=True)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return

                elif action_data.get("action") == "shopping":
                    item_name = action_data.get('item_name', '未知物品')
                    quantity = action_data.get('quantity', '')
                    qty_text = f" ({quantity})" if quantity else ""
                    
                    embed = discord.Embed(title="🛒 AI 偵測到採購需求", description="請問是否加入採購清單？", color=discord.Color.blue())
                    embed.add_field(name="採買項目", value=f"{item_name}{qty_text}", inline=False)
                    
                    view = AIActionConfirmView(action_data, message.guild.id)
                    await message.reply(embed=embed, view=view)
                    return
                    
            except json.JSONDecodeError:
                await message.reply("❌ AI 回傳的資料格式解析失敗。")

async def setup(bot):
    await bot.add_cog(AIAgentCog(bot))

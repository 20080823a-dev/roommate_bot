import discord
from discord.ext import commands
from discord import app_commands
import uuid
from database.db_manager import db
from utils.debt_calc import calculate_minimum_transactions

class FinanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    finance = app_commands.Group(name="finance", description="記帳與費用分攤指令")

    @finance.command(name="expense", description="新增分攤消費 (全體均分，餘數付款人吸收)")
    @app_commands.describe(title="消費名稱", amount="總金額", payer="先墊錢的人", exclude="不參與分攤的人(可選)")
    async def expense(self, interaction: discord.Interaction, title: str, amount: int, payer: discord.Member, exclude: discord.Member = None):
        await interaction.response.defer(ephemeral=False)
        gid = interaction.guild_id
        
        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", gid)
        if not rms:
            return await interaction.followup.send("❌ 尚未登記室友。")
        
        participating_rms = [r['user_id'] for r in rms if not (exclude and r['user_id'] == exclude.id)]
        if not participating_rms:
            return await interaction.followup.send("❌ 排除後沒有人可以分攤了。")

        split = amount // len(participating_rms)
        
        async with db.transaction() as conn:
            eid = await conn.fetchval(
                "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                gid, title, amount, interaction.user.id
            )
            for uid in participating_rms:
                if uid != payer.id:
                    await conn.execute(
                        "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                        gid, eid, uid, payer.id, split
                    )
        
        exclude_text = f" (已排除 <@{exclude.id}>)" if exclude else ""
        await interaction.followup.send(f"✅ **{title}** (${amount}) 已記帳，由 <@{payer.id}> 先付{exclude_text}。其他人各需給付 ${split}。")

    @finance.command(name="pay", description="資金轉交 (還錢給墊款人)")
    @app_commands.describe(payer="付錢的人", payee="收錢的人", amount="金額")
    async def pay(self, interaction: discord.Interaction, payer: discord.Member, payee: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=False)
        async with db.transaction() as conn:
            eid = await conn.fetchval(
                "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, '資金轉交', $2, $3) RETURNING id",
                interaction.guild_id, amount, interaction.user.id
            )
            await conn.execute(
                "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                interaction.guild_id, eid, payee.id, payer.id, amount
            )
        await interaction.followup.send(f"✅ 資金轉交：<@{payer.id}> 已向 <@{payee.id}> 支付 ${amount}。")

    @finance.command(name="undo", description="撤銷自己建立的有效記帳")
    @app_commands.describe(event_id="要撤銷的紀錄")
    async def undo(self, interaction: discord.Interaction, event_id: str):
        await interaction.response.defer(ephemeral=True)
        async with db.transaction() as conn:
            res = await conn.execute(
                "UPDATE expense_events SET is_deleted = true WHERE id = $1 AND guild_id = $2 AND created_by = $3 AND is_deleted = false",
                uuid.UUID(event_id), interaction.guild_id, interaction.user.id
            )
            if res != "UPDATE 0":
                await conn.execute("UPDATE ledger SET is_deleted = true WHERE event_id = $1", uuid.UUID(event_id))
                return await interaction.followup.send("✅ 記帳撤銷成功。")
        await interaction.followup.send("❌ 撤銷失敗，權限不足或查無此紀錄。")

    @undo.autocomplete("event_id")
    async def undo_auto(self, interaction: discord.Interaction, current: str):
        recs = await db.fetch(
            "SELECT id, description, total_amount FROM expense_events WHERE guild_id = $1 AND created_by = $2 AND is_deleted = false ORDER BY created_at DESC LIMIT 5",
            interaction.guild_id, interaction.user.id
        )
        return [app_commands.Choice(name=f"{r['description']} (${r['total_amount']})", value=str(r['id'])) for r in recs if current.lower() in r['description'].lower()][:25]

    @finance.command(name="history", description="查詢本伺服器最近 10 筆未刪除的記帳紀錄與明細")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        
        # 使用 CTE 限制事件數量為 10，再 JOIN ledger 取得明細 (INV-1, INV-5)
        query = """
        WITH RecentEvents AS (
            SELECT id, description, total_amount, created_by, created_at
            FROM expense_events
            WHERE guild_id = $1 AND is_deleted = false
            ORDER BY created_at DESC
            LIMIT 10
        )
        SELECT r.id, r.description, r.total_amount, r.created_by, r.created_at,
               l.debtor_id, l.creditor_id, l.amount as debt_amount
        FROM RecentEvents r
        LEFT JOIN ledger l ON r.id = l.event_id AND l.is_deleted = false
        ORDER BY r.created_at DESC
        """
        records = await db.fetch(query, interaction.guild_id)

        if not records:
            return await interaction.followup.send("目前沒有任何記帳紀錄。")

        # 資料整併：把多筆帳務明細收到對應的 Event ID 底下
        events = {}
        event_order = []
        for r in records:
            eid = r['id']
            if eid not in events:
                events[eid] = {
                    "desc": r['description'],
                    "total": r['total_amount'],
                    "creator": r['created_by'],
                    "details": []
                }
                event_order.append(eid)
            
            if r['debtor_id'] and r['creditor_id'] and r['debt_amount']:
                events[eid]['details'].append(
                    f"  ↳ <@{r['debtor_id']}> 需給 <@{r['creditor_id']}> : `${r['debt_amount']}`"
                )

        lines = []
        for eid in event_order:
            data = events[eid]
            lines.append(f"• **{data['desc']}** (${data['total']}) - <@{data['creator']}> 建立")
            if data['details']:
                lines.extend(data['details'])
            else:
                lines.append("  ↳ *(無分攤明細 / 自行吸收)*")

        content = "**📜 最近 10 筆記帳紀錄與明細：**\n" + "\n".join(lines)
        
        # 預防超過 Discord 單則訊息 2000 字元限制
        if len(content) > 2000:
            content = content[:1995] + "..."
            
        await interaction.followup.send(content)

    @finance.command(name="balance", description="結算本伺服器目前的帳務，並提供最少筆數的還款建議")
    async def balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild_id

        records = await db.fetch(
            "SELECT debtor_id, creditor_id, amount FROM ledger WHERE guild_id = $1 AND is_deleted = false",
            guild_id
        )

        if not records:
            return await interaction.followup.send("🎉 目前沒有任何未結清的帳務！")

        net_balances = {}
        for r in records:
            debtor = r['debtor_id']
            creditor = r['creditor_id']
            amount = r['amount']

            net_balances[debtor] = net_balances.get(debtor, 0) - amount
            net_balances[creditor] = net_balances.get(creditor, 0) + amount

        net_balances = {uid: amt for uid, amt in net_balances.items() if amt != 0}

        if not net_balances:
            return await interaction.followup.send("🎉 帳務剛剛好互相抵銷，目前無人欠款！")

        transactions = calculate_minimum_transactions(net_balances)

        if not transactions:
            return await interaction.followup.send("🎉 目前帳務已結清！")

        lines = []
        for debtor_id, creditor_id, amount in transactions:
            lines.append(f"• <@{debtor_id}> 應支付給 <@{creditor_id}>：**${amount}**")

        await interaction.followup.send(f"📊 **本伺服器最佳還款建議 (最少筆數)：**\n" + "\n".join(lines))

async def setup(bot):
    await bot.add_cog(FinanceCog(bot))
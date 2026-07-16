# cogs/finance.py
import discord
from discord.ext import commands
from discord import app_commands
import uuid
import re
from database.db_manager import db
from utils.debt_calc import calculate_minimum_transactions
from utils.ui import PaginationView

class FinanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    finance = app_commands.Group(name="finance", description="記帳與費用分攤指令")

    @finance.command(name="expense", description="新增分攤消費 (僅由標記的人平分)")
    @app_commands.describe(
        title="消費名稱", 
        amount="總金額", 
        participants="請標記所有要平分的人 (例如: @A @B)",
        payer="代墊人 (預設為指令發起人)"
    )
    @finance.command(name="expense", description="新增分攤消費 (僅由標記的人平分)")
    @app_commands.describe(
        title="消費名稱", 
        amount="總金額", 
        participants="請標記所有要平分的人 (例如: @A @B)",
        payer="代墊人 (預設為指令發起人)"
    )
    async def expense(
        self, 
        interaction: discord.Interaction, 
        title: str, 
        amount: int, 
        participants: str,
        payer: discord.Member = None
    ):
        await interaction.response.defer(ephemeral=False)
        
        # 🛡️ 防呆 1：金額檢查
        if amount <= 0:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 錯誤：金額必須大於 0！", color=discord.Color.red()))
            
        gid = interaction.guild_id
        payer_id = payer.id if payer else interaction.user.id
        
        # 1. 萃取字串中的所有 Discord ID，並去除重複
        tagged_ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', participants)]
        participating_rms = list(set(tagged_ids)) 
        
        # 🛡️ 防呆 2：人數檢查
        if not participating_rms:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 錯誤：請在 participants 欄位中 @標記 至少一位成員！", color=discord.Color.red()))
        if len(participating_rms) == 1 and participating_rms[0] == payer_id:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 錯誤：不能只有代墊人自己一個人分攤！", color=discord.Color.red()))

        split = amount // len(participating_rms)
        
        # 2. 寫入資料庫 (加上 try-except 保護)
        try:
            async with db.transaction() as conn:
                eid = await conn.fetchval(
                    "INSERT INTO expense_events (guild_id, description, total_amount, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                    gid, title, amount, interaction.user.id
                )
                for uid in participating_rms:
                    if uid != payer_id:
                        await conn.execute(
                            "INSERT INTO ledger (guild_id, event_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, $4, $5)",
                            gid, eid, uid, payer_id, split
                        )
            
            embed = discord.Embed(title="🧾 新增消費成功", color=discord.Color.green())
            embed.add_field(name="項目", value=title, inline=True)
            embed.add_field(name="總金額", value=f"${amount}", inline=True)
            embed.add_field(name="代墊人", value=f"<@{payer_id}>", inline=False)
            embed.add_field(name="分攤結果", value=f"共 {len(participating_rms)} 人參與平分，其他人各需給付代墊人 **${split}**", inline=False)
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(description=f"❌ 資料庫寫入失敗，請聯絡管理員：{e}", color=discord.Color.red()))

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
        
        embed = discord.Embed(title="💸 資金轉交成功", description=f"<@{payer.id}> 已向 <@{payee.id}> 支付 **${amount}**", color=discord.Color.teal())
        await interaction.followup.send(embed=embed)

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
                return await interaction.followup.send(embed=discord.Embed(description="✅ 記帳撤銷成功。", color=discord.Color.green()))
        await interaction.followup.send(embed=discord.Embed(description="❌ 撤銷失敗，權限不足或查無此紀錄。", color=discord.Color.red()))

    @undo.autocomplete("event_id")
    async def undo_auto(self, interaction: discord.Interaction, current: str):
        recs = await db.fetch(
            "SELECT id, description, total_amount FROM expense_events WHERE guild_id = $1 AND created_by = $2 AND is_deleted = false ORDER BY created_at DESC LIMIT 5",
            interaction.guild_id, interaction.user.id
        )
        return [app_commands.Choice(name=f"{r['description']} (${r['total_amount']})", value=str(r['id'])) for r in recs if current.lower() in r['description'].lower()][:25]

    @finance.command(name="history", description="查詢本伺服器最近 50 筆未刪除的記帳紀錄 (支援分頁)")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        
        query = """
        WITH RecentEvents AS (
            SELECT id, description, total_amount, created_by, created_at
            FROM expense_events
            WHERE guild_id = $1 AND is_deleted = false
            ORDER BY created_at DESC
            LIMIT 50
        )
        SELECT r.id, r.description, r.total_amount, r.created_by, r.created_at,
               l.debtor_id, l.creditor_id, l.amount as debt_amount
        FROM RecentEvents r
        LEFT JOIN ledger l ON r.id = l.event_id AND l.is_deleted = false
        ORDER BY r.created_at DESC
        """
        records = await db.fetch(query, interaction.guild_id)

        if not records:
            return await interaction.followup.send(embed=discord.Embed(description="目前沒有任何記帳紀錄。", color=discord.Color.light_gray()))

        events = {}
        event_order = []
        for r in records:
            eid = r['id']
            if eid not in events:
                events[eid] = {
                    "desc": r['description'],
                    "total": r['total_amount'],
                    "creator": r['created_by'],
                    "time": r['created_at'].strftime("%Y-%m-%d %H:%M"),
                    "details": []
                }
                event_order.append(eid)
            
            if r['debtor_id'] and r['creditor_id'] and r['debt_amount']:
                events[eid]['details'].append(f"↳ <@{r['debtor_id']}> 給 <@{r['creditor_id']}> : `${r['debt_amount']}`")

        ITEMS_PER_PAGE = 5
        pages_data = [event_order[i:i + ITEMS_PER_PAGE] for i in range(0, len(event_order), ITEMS_PER_PAGE)]
        
        embeds = []
        for i, page_keys in enumerate(pages_data):
            embed = discord.Embed(title="📜 歷史記帳紀錄", color=discord.Color.blue())
            embed.set_footer(text=f"頁數 {i+1} / {len(pages_data)}")
            
            for eid in page_keys:
                data = events[eid]
                detail_str = "\n".join(data['details']) if data['details'] else "*(無分攤明細 / 自行吸收)*"
                embed.add_field(
                    name=f"🛒 {data['desc']} (${data['total']})",
                    value=f"建立者: <@{data['creator']}>\n{detail_str}",
                    inline=False
                )
            embeds.append(embed)

        if len(embeds) > 1:
            view = PaginationView(embeds)
            await interaction.followup.send(embed=embeds[0], view=view)
        else:
            await interaction.followup.send(embed=embeds[0])

    @finance.command(name="balance", description="結算本伺服器目前的帳務，並提供最少筆數的還款建議")
    async def balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild_id

        records = await db.fetch(
            "SELECT debtor_id, creditor_id, amount FROM ledger WHERE guild_id = $1 AND is_deleted = false",
            guild_id
        )

        if not records:
            return await interaction.followup.send(embed=discord.Embed(description="🎉 目前沒有任何未結清的帳務！", color=discord.Color.green()))

        net_balances = {}
        for r in records:
            debtor = r['debtor_id']
            creditor = r['creditor_id']
            amount = r['amount']
            net_balances[debtor] = net_balances.get(debtor, 0) - amount
            net_balances[creditor] = net_balances.get(creditor, 0) + amount

        net_balances = {uid: amt for uid, amt in net_balances.items() if amt != 0}

        if not net_balances:
            return await interaction.followup.send(embed=discord.Embed(description="🎉 帳務剛剛好互相抵銷，目前無人欠款！", color=discord.Color.green()))

        transactions = calculate_minimum_transactions(net_balances)

        if not transactions:
            return await interaction.followup.send(embed=discord.Embed(description="🎉 目前帳務已結清！", color=discord.Color.green()))

        embed = discord.Embed(title="📊 本伺服器最佳還款建議", description="演算法已將三角債務化簡為「最少還款筆數」", color=discord.Color.gold())
        
        for debtor_id, creditor_id, amount in transactions:
            embed.add_field(
                name="💰 應付帳款", 
                value=f"<@{debtor_id}> ➡️ <@{creditor_id}>\n**金額：${amount}**", 
                inline=False
            )

        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(FinanceCog(bot))

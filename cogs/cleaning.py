import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, timezone
from database.db_manager import db
import logging

logger = logging.getLogger("bot.cleaning")

class CleaningCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    # 💡 專屬錯誤攔截：以後如果還有錯，就能直接看到是什麼錯
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Cleaning 指令錯誤: {error}")
        err_msg = f"❌ 系統錯誤：{error}"
        if not interaction.response.is_done():
            await interaction.response.send_message(err_msg, ephemeral=True)
        else:
            await interaction.followup.send(err_msg, ephemeral=True)
    
    clean = app_commands.Group(name="clean", description="打掃輪值與代班系統")

    @clean.command(name="set_interval", description="[管理員] 設定打掃換班週期與啟用輪值")
    @app_commands.describe(days="幾天換班一次？")
    @app_commands.default_permissions(manage_guild=True)
    async def set_interval(self, interaction: discord.Interaction, days: int):
        await interaction.response.defer()
        
        # 防呆：避免輸入 0 導致日後運算除以零崩潰
        if days <= 0:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 天數必須大於 0！", color=discord.Color.red()))
            
        next_date = datetime.now(timezone.utc) + timedelta(days=days)
        
        # 安全寫法：先查詢是否存在，避免 ON CONFLICT 依賴特定約束設定
        async with db.transaction() as conn:
            exists = await conn.fetchval("SELECT guild_id FROM cleaning_schedules WHERE guild_id = $1", interaction.guild_id)
            if exists:
                await conn.execute("UPDATE cleaning_schedules SET next_rotation_at = $1, rotation_interval_days = $2 WHERE guild_id = $3", next_date, days, interaction.guild_id)
            else:
                await conn.execute("INSERT INTO cleaning_schedules (guild_id, next_rotation_at, rotation_interval_days) VALUES ($1, $2, $3)", interaction.guild_id, next_date, days)

        await interaction.followup.send(embed=discord.Embed(description=f"✅ 已設定每 **{days}** 天換班一次！", color=discord.Color.green()))

    @clean.command(name="status", description="查看本期打掃值星官")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        rms = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1 ORDER BY joined_at", interaction.guild_id)
        if not rms:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 尚未登記室友。", color=discord.Color.red()))
        
        sch = await db.fetchrow("SELECT next_rotation_at, rotation_interval_days FROM cleaning_schedules WHERE guild_id = $1", interaction.guild_id)
        if not sch:
            return await interaction.followup.send(embed=discord.Embed(description="⚠️ 尚未設定打掃排程 (請管理員使用 /clean set_interval)。", color=discord.Color.orange()))

        now = datetime.now(timezone.utc)
        days_until_next = (sch['next_rotation_at'] - now).days
        total_rotations = (now - datetime(2020,1,1, tzinfo=timezone.utc)).days // sch['rotation_interval_days']
        current_duty_idx = total_rotations % len(rms)
        current_duty_uid = rms[current_duty_idx]['user_id']

        embed = discord.Embed(title="🧹 本期打掃值星官", color=discord.Color.blue())
        embed.add_field(name="當前負責人", value=f"👑 <@{current_duty_uid}>", inline=False)
        embed.add_field(name="下次換班時間", value=f"{sch['next_rotation_at'].strftime('%Y-%m-%d')} (約 {max(0, days_until_next)} 天後)", inline=False)
        
        await interaction.followup.send(embed=embed)

    @clean.command(name="skip", description="請人代班打掃 (會記錄欠對方一次)")
    @app_commands.describe(substitute="幫你代班的人")
    async def skip(self, interaction: discord.Interaction, substitute: discord.Member):
        await interaction.response.defer()
        if interaction.user.id == substitute.id:
            return await interaction.followup.send(embed=discord.Embed(description="❌ 你不能找自己代班！", color=discord.Color.red()))
        
        # 已將 debt_count 改為正確的 amount，並使用安全寫法
        async with db.transaction() as conn:
            record = await conn.fetchrow(
                "SELECT amount FROM cleaning_debts WHERE guild_id = $1 AND debtor_id = $2 AND creditor_id = $3",
                interaction.guild_id, interaction.user.id, substitute.id
            )
            if record:
                await conn.execute(
                    "UPDATE cleaning_debts SET amount = amount + 1 WHERE guild_id = $1 AND debtor_id = $2 AND creditor_id = $3",
                    interaction.guild_id, interaction.user.id, substitute.id
                )
            else:
                await conn.execute(
                    "INSERT INTO cleaning_debts (guild_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, 1)",
                    interaction.guild_id, interaction.user.id, substitute.id
                )

        await interaction.followup.send(embed=discord.Embed(description=f"✅ 已紀錄！<@{interaction.user.id}> 欠 <@{substitute.id}> **1** 次打掃。", color=discord.Color.green()))

    @clean.command(name="debts", description="查看大家積欠的代班次數")
    async def debts(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 已將 debt_count 改為正確的 amount
        recs = await db.fetch("SELECT debtor_id, creditor_id, amount FROM cleaning_debts WHERE guild_id = $1 AND amount > 0", interaction.guild_id)
        
        if not recs:
            return await interaction.followup.send(embed=discord.Embed(description="🎉 目前沒有人欠打掃代班！", color=discord.Color.green()))
        
        embed = discord.Embed(title="📋 打掃代班債務表", description="請欠班的人記得下次幫忙掃喔！", color=discord.Color.dark_orange())
        for r in recs:
            # 💡 修復空字串報錯：加入 "欠債明細" 作為標題
            embed.add_field(
                name="欠債明細", 
                value=f"• <@{r['debtor_id']}> 欠 <@{r['creditor_id']}> : **{r['amount']} 次**", 
                inline=False
            )
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CleaningCog(bot))

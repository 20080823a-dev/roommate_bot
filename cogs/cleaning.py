import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database.db_manager import db
from utils.checks import is_admin

logger = logging.getLogger("bot.cleaning")
tz = ZoneInfo("Asia/Taipei")

class CleaningCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 監聽 main.py 發出的排程事件 (INV-4)
    @commands.Cog.listener("on_cleaning_cycle")
    async def process_cleaning_cycle(self, guild_id: int):
        try:
            schedule = await db.fetchrow("SELECT * FROM cleaning_schedules WHERE guild_id = $1", guild_id)
            if not schedule or not schedule['next_rotation_at']:
                return

            now = datetime.now(tz)
            next_rot = schedule['next_rotation_at'].astimezone(tz)
            
            if now >= next_rot:
                interval = schedule['rotation_interval_days']
                diff_days = (now - next_rot).days
                cycles = (diff_days // interval) + 1
                
                # 換班時間繼續保持在 00:00
                new_next_rot = next_rot + timedelta(days=cycles * interval)

                roommates = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1 ORDER BY joined_at ASC", guild_id)
                if not roommates:
                    return
                
                rm_ids = [r['user_id'] for r in roommates]
                curr_user = schedule.get('current_turn_user_id')
                
                try:
                    curr_idx = rm_ids.index(curr_user) if curr_user else 0
                except ValueError:
                    curr_idx = 0
                    
                # 依據「原定值星官」推演下一位，不受代班影響
                new_idx = (curr_idx + cycles) % len(rm_ids)
                new_user = rm_ids[new_idx]

                # 換班時，清空上週期的代班人狀態 (substitute_user_id = NULL)
                await db.execute(
                    "UPDATE cleaning_schedules SET next_rotation_at = $1, current_turn_user_id = $2, substitute_user_id = NULL WHERE guild_id = $3",
                    new_next_rot, new_user, guild_id
                )
                logger.info(f"伺服器 {guild_id} 觸發換班，輪到 {new_user}", extra={"guild_id": guild_id})
        except Exception as e:
            logger.error(f"換班失敗: {e}", exc_info=e, extra={"guild_id": guild_id})

    clean_group = app_commands.Group(name="clean", description="打掃輪值指令")

    @clean_group.command(name="set_interval", description="[管理員] 設定打掃換班週期與啟用輪值")
    @is_admin()
    async def set_interval(self, interaction: discord.Interaction, days: int):
        await interaction.response.defer(ephemeral=True)
        if days <= 0:
            await interaction.followup.send("❌ 天數必須大於 0。")
            return
            
        guild_id = interaction.guild_id
        now = datetime.now(tz)
        
        # 將下次換班時間強制歸零至目標日期的 00:00:00
        next_rot = (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        first_rm = await db.fetchval("SELECT user_id FROM roommates WHERE guild_id = $1 ORDER BY joined_at ASC LIMIT 1", guild_id)
        if not first_rm:
            await interaction.followup.send("❌ 伺服器內尚未加入任何室友。")
            return

        await db.execute(
            """
            INSERT INTO cleaning_schedules (guild_id, next_rotation_at, rotation_interval_days, current_turn_user_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id) DO UPDATE 
            SET rotation_interval_days = $3, 
                next_rotation_at = $2, 
                current_turn_user_id = COALESCE(cleaning_schedules.current_turn_user_id, $4),
                substitute_user_id = NULL
            """,
            guild_id, next_rot, days, first_rm
        )
        # 只顯示年月日
        await interaction.followup.send(f"✅ 輪值已啟用！每 `{days}` 天換班一次。下次換班：{next_rot.strftime('%Y-%m-%d')}")

    @clean_group.command(name="status", description="查看目前輪到誰打掃")
    async def status(self, interaction: discord.Interaction):
        schedule = await db.fetchrow("SELECT * FROM cleaning_schedules WHERE guild_id = $1", interaction.guild_id)
        if not schedule or not schedule['current_turn_user_id']:
            await interaction.response.send_message("ℹ️ 本伺服器尚未設定打掃輪值。", ephemeral=True)
            return
            
        curr_user = schedule['current_turn_user_id']
        sub_user = schedule.get('substitute_user_id')
        
        # 判斷是否有代班人介入
        display_user = f"<@{sub_user}> (代 <@{curr_user}> 班)" if sub_user else f"<@{curr_user}>"
        
        # 只顯示年月日
        next_rot = schedule['next_rotation_at'].astimezone(tz).strftime('%Y-%m-%d')
        await interaction.response.send_message(
            f"🧹 **本期打掃值星**：{display_user}\n"
            f"⏱️ **下次換班日期**：{next_rot}\n"
            f"🔄 **週期**：每 {schedule['rotation_interval_days']} 天"
        )

    @clean_group.command(name="skip", description="登記別人幫你代班一次 (欠次紀錄)")
    @app_commands.describe(helper_id="幫你代班的人")
    async def skip(self, interaction: discord.Interaction, helper_id: str):
        await interaction.response.defer(ephemeral=False)
        creditor_id = int(helper_id)
        debtor_id = interaction.user.id
        
        if creditor_id == debtor_id:
            await interaction.followup.send("❌ 你不能幫自己代班。")
            return

        schedule = await db.fetchrow("SELECT current_turn_user_id FROM cleaning_schedules WHERE guild_id = $1", interaction.guild_id)

        # Transaction 原子寫入，保證欠次紀錄與代班狀態同時成功 (INV-2)
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO cleaning_debts (guild_id, debtor_id, creditor_id, amount) VALUES ($1, $2, $3, 1)",
                interaction.guild_id, debtor_id, creditor_id
            )
            
            # 若提出代班申請的人，剛好是「本期值星官」，則同時更新代班顯示狀態
            is_current_turn = schedule and schedule['current_turn_user_id'] == debtor_id
            if is_current_turn:
                await conn.execute(
                    "UPDATE cleaning_schedules SET substitute_user_id = $1 WHERE guild_id = $2",
                    creditor_id, interaction.guild_id
                )
        
        msg = f"🤝 紀錄成功！<@{debtor_id}> 欠 <@{creditor_id}> 一次代班。"
        if is_current_turn:
            msg += f"\n👉 本期打掃已更新為 <@{creditor_id}> 執行。"
            
        await interaction.followup.send(msg)

    @skip.autocomplete("helper_id")
    async def skip_autocomplete(self, interaction: discord.Interaction, current: str):
        records = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1 AND user_id != $2", interaction.guild_id, interaction.user.id)
        choices = []
        for r in records:
            uid = r['user_id']
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else str(uid)
            if current.lower() in name.lower() or current in str(uid):
                choices.append(app_commands.Choice(name=name, value=str(uid)))
        return choices[:25]

    @clean_group.command(name="debts", description="查看本伺服器的所有代班欠次")
    async def debts(self, interaction: discord.Interaction):
        records = await db.fetch(
            "SELECT debtor_id, creditor_id, SUM(amount) as total FROM cleaning_debts WHERE guild_id = $1 GROUP BY debtor_id, creditor_id",
            interaction.guild_id
        )
        if not records:
            await interaction.response.send_message("🎉 目前沒有任何欠次紀錄！", ephemeral=False)
            return
        
        lines = [f"• <@{r['debtor_id']}> 欠 <@{r['creditor_id']}> : `{r['total']}` 次" for r in records]
        await interaction.response.send_message("**📊 代班欠次總覽：**\n" + "\n".join(lines))

async def setup(bot):
    await bot.add_cog(CleaningCog(bot))
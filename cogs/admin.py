import discord
from discord.ext import commands
from discord import app_commands
import logging
from database.db_manager import db
import config
from utils.checks import is_admin

logger = logging.getLogger("bot.admin")

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 建立 /roommate 指令群組
    roommate_group = app_commands.Group(name="roommate", description="室友名單管理指令")

    @roommate_group.command(name="add", description="[管理員] 新增室友至本伺服器的名單中")
    @app_commands.describe(member="要加入的伺服器成員")
    @is_admin()
    async def roommate_add(self, interaction: discord.Interaction, member: discord.Member):
        # 統一 ephemeral 策略：錯誤/權限/管理操作皆設為隱藏
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        # 檢查是否超過 MAX_ROOMMATES_PER_GUILD 限制 (多租戶防護)
        current_count = await db.fetchval("SELECT COUNT(*) FROM roommates WHERE guild_id = $1", guild_id)
        if current_count >= config.MAX_ROOMMATES_PER_GUILD:
            await interaction.followup.send(f"❌ 拒絕新增：本伺服器的室友數量已達上限 ({config.MAX_ROOMMATES_PER_GUILD} 人)。", ephemeral=True)
            return

        try:
            # [自我修復機制] 確保該伺服器確實存在於 guilds 表，防禦 Foreign Key 錯誤 (INV-4)
            await db.execute(
                """
                INSERT INTO guilds (guild_id, is_active, joined_at) 
                VALUES ($1, true, NOW()) 
                ON CONFLICT (guild_id) DO UPDATE SET is_active = true
                """,
                guild_id
            )

            # 使用 ON CONFLICT DO NOTHING 防禦重複加入
            result = await db.execute(
                "INSERT INTO roommates (guild_id, user_id, joined_at) VALUES ($1, $2, NOW()) ON CONFLICT DO NOTHING",
                guild_id, member.id
            )
            if result == "INSERT 0":
                await interaction.followup.send(f"⚠️ {member.display_name} 已經在室友名單中了。", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ 成功將 {member.display_name} 加入本伺服器的室友名單！", ephemeral=True)
        except Exception as e:
            # 加入 logger 避免未來被籠統的錯誤訊息蒙蔽雙眼
            logger.error(f"新增室友失敗: {e}", exc_info=e, extra={"guild_id": guild_id})
            await interaction.followup.send(f"❌ 新增失敗：發生資料庫錯誤，請查看終端機日誌。", ephemeral=True)

    @roommate_group.command(name="remove", description="[管理員] 將室友從本伺服器名單中移除")
    @app_commands.describe(user_id="要移除的室友 (請從選單挑選)")
    @is_admin()
    async def roommate_remove(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        target_id = int(user_id)

        deleted = await db.execute("DELETE FROM roommates WHERE guild_id = $1 AND user_id = $2", guild_id, target_id)
        if deleted == "DELETE 0":
            await interaction.followup.send("❌ 找不到該名室友，請確認對方是否在名單內。", ephemeral=True)
        else:
            await interaction.followup.send("✅ 已成功將該名室友從名單中移除。", ephemeral=True)

    @roommate_remove.autocomplete("user_id")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # 動態選項防呆 (INV-1: 絕對僅限本伺服器)
        guild_id = interaction.guild_id
        if not guild_id:
            return []
        
        records = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", guild_id)
        choices = []
        for r in records:
            uid = r['user_id']
            member = interaction.guild.get_member(uid)
            # 若無快取，退回顯示 ID 作為備案
            name = member.display_name if member else f"未知用戶 ({uid})" 
            
            if current.lower() in name.lower() or current in str(uid):
                choices.append(app_commands.Choice(name=name, value=str(uid)))
        return choices[:25]

    @roommate_group.command(name="list", description="列出本伺服器所有登記的室友")
    async def roommate_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild_id
        
        records = await db.fetch("SELECT user_id FROM roommates WHERE guild_id = $1", guild_id)
        if not records:
            await interaction.followup.send("目前本伺服器還沒有加入任何室友。")
            return
        
        names = [f"• <@{r['user_id']}>" for r in records]
        await interaction.followup.send(
            f"📋 **本伺服器室友名單** ({len(names)}/{config.MAX_ROOMMATES_PER_GUILD}):\n" + "\n".join(names)
        )

async def setup(bot):
    await bot.add_cog(AdminCog(bot))
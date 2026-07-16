# cogs/shopping.py
import discord
from discord.ext import commands
from discord import app_commands
from database.db_manager import db
import logging

logger = logging.getLogger("bot.shopping")

class ShoppingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Shopping 指令錯誤: {error}")
        err_msg = f"❌ 採購指令執行失敗：{error}"
        if not interaction.response.is_done():
            await interaction.response.send_message(err_msg, ephemeral=True)
        else:
            await interaction.followup.send(err_msg, ephemeral=True)

    shopping = app_commands.Group(name="shopping", description="採購清單管理指令")

    @shopping.command(name="add", description="手動新增物品到採購清單")
    @app_commands.describe(item="要買的物品名稱與數量")
    async def add_item(self, interaction: discord.Interaction, item: str):
        await interaction.response.defer()
        
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO shopping_list (guild_id, item_name, added_by) VALUES ($1, $2, $3)",
                interaction.guild_id, item, interaction.user.id
            )
            
        embed = discord.Embed(title="🛒 採購清單已更新", description=f"已成功將 **{item}** 加入清單！", color=discord.Color.green())
        await interaction.followup.send(embed=embed)

    @shopping.command(name="list", description="查看目前的待採購清單")
    async def list_items(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        records = await db.fetch(
            "SELECT id, item_name, added_by, created_at FROM shopping_list WHERE guild_id = $1 ORDER BY created_at ASC",
            interaction.guild_id
        )
        
        if not records:
            return await interaction.followup.send(embed=discord.Embed(description="🎉 目前採購清單是空的！不需要買東西。", color=discord.Color.light_gray()))
            
        embed = discord.Embed(title="🛒 待採購清單", description="以下是目前需要購買的物品：", color=discord.Color.blue())
        
        for idx, r in enumerate(records, 1):
            time_str = r['created_at'].strftime("%m/%d %H:%M")
            embed.add_field(
                name=f"{idx}. {r['item_name']} (ID: {r['id']})", 
                value=f"由 <@{r['added_by']}> 新增於 {time_str}", 
                inline=False
            )
            
        await interaction.followup.send(embed=embed)

    @shopping.command(name="remove", description="從採購清單移除已買好或不需要的物品")
    @app_commands.describe(item_id="物品的 ID (可透過 /shopping list 查詢)")
    async def remove_item(self, interaction: discord.Interaction, item_id: int):
        await interaction.response.defer()
        
        res = await db.execute(
            "DELETE FROM shopping_list WHERE id = $1 AND guild_id = $2",
            item_id, interaction.guild_id
        )
        
        if res == "DELETE 0":
            await interaction.followup.send(embed=discord.Embed(description="❌ 找不到該 ID 的物品，或者該物品不屬於本伺服器。", color=discord.Color.red()))
        else:
            await interaction.followup.send(embed=discord.Embed(description=f"✅ 已成功將 ID `{item_id}` 的物品從採購清單中移除！", color=discord.Color.green()))

async def setup(bot):
    await bot.add_cog(ShoppingCog(bot))

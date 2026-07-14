import discord
from discord.ext import commands
from discord import app_commands
import logging
from database.db_manager import db

logger = logging.getLogger("bot.shopping")

class PurchaseButton(discord.ui.Button):
    def __init__(self, item_id: str, item_name: str):
        # 限制標籤長度，避免超過 Discord UI 限制
        display_name = (item_name[:70] + '..') if len(item_name) > 70 else item_name
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"✔ 買 {display_name}",
            # custom_id 內嵌資料庫 UUID，確保全域唯一、多伺服器絕對不誤觸 (INV-1)
            custom_id=f"buy_{item_id}"
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction):
        # INV-2: 原子性條件更新 (Atomic Update)。不先 SELECT，直接嘗試 UPDATE
        async with db.transaction() as conn:
            res = await conn.fetchval(
                """
                UPDATE shopping_items 
                SET is_purchased = true 
                WHERE id = $1 AND is_purchased = false 
                RETURNING id
                """,
                self.item_id
            )
            
            if not res:
                # 若 UPDATE 沒回傳 ID，代表這瞬間條件已不成立（被別人買走了）
                await interaction.response.send_message("⚠️ 手腳太慢啦！這項物品已經有人先標記購買了。", ephemeral=True)
                return
        
        # 更新成功，修改按鈕外觀為停用狀態
        self.disabled = True
        self.style = discord.ButtonStyle.secondary
        self.label = f"已由 {interaction.user.display_name} 購買"
        
        # 重新渲染當前 View
        await interaction.response.edit_message(view=self.view)

class ShoppingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    buy_group = app_commands.Group(name="buy", description="公用品採購清單指令")

    @buy_group.command(name="add", description="新增公用品至採購清單")
    @app_commands.describe(item_name="物品名稱", quantity="數量(可選，例如: 2串)")
    async def add_item(self, interaction: discord.Interaction, item_name: str, quantity: str = ""):
        await interaction.response.defer(ephemeral=False)
        
        async with db.transaction() as conn:
            item_id = await conn.fetchval(
                """
                INSERT INTO shopping_items (guild_id, item_name, quantity, added_by) 
                VALUES ($1, $2, $3, $4) RETURNING id
                """,
                interaction.guild_id, item_name, quantity, interaction.user.id
            )
        
        # 建立一個沒有逾時限制的 View，以確保持久化
        view = discord.ui.View(timeout=None)
        view.add_item(PurchaseButton(str(item_id), item_name))
        
        qty_text = f" ({quantity})" if quantity else ""
        await interaction.followup.send(
            f"🛒 **新增待採購**：{item_name}{qty_text} \n*(由 <@{interaction.user.id}> 提出)*", 
            view=view
        )

    @buy_group.command(name="list", description="列出本伺服器所有未採購的公用品")
    async def list_items(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        
        # 為了避免單一 View 按鈕超載，這裡限制最多撈取 25 筆
        records = await db.fetch(
            """
            SELECT id, item_name, quantity FROM shopping_items 
            WHERE guild_id = $1 AND is_purchased = false 
            ORDER BY added_at ASC LIMIT 25
            """,
            interaction.guild_id
        )
        
        if not records:
            return await interaction.followup.send("🎉 目前沒有任何待採購的公用品！")
        
        view = discord.ui.View(timeout=None)
        lines = []
        for i, r in enumerate(records, 1):
            qty_text = f" ({r['quantity']})" if r['quantity'] else ""
            lines.append(f"{i}. **{r['item_name']}**{qty_text}")
            view.add_item(PurchaseButton(str(r['id']), r['item_name']))
        
        content = f"📋 **本伺服器待採購清單** (點擊下方按鈕勾選)：\n" + "\n".join(lines)
        await interaction.followup.send(content, view=view)

async def setup(bot):
    await bot.add_cog(ShoppingCog(bot))
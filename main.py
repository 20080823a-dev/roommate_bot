import os
import logging
import asyncio
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from database.db_manager import db

# 1. 基礎結構化設定日誌 (使用 Filter 完美避開 KeyError)
class GuildIDFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "guild_id"):
            record.guild_id = "Global"
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s (Guild: %(guild_id)s): %(message)s"
)

# 將過濾器掛載到系統預設處理器
for handler in logging.root.handlers:
    handler.addFilter(GuildIDFilter())

logger = logging.getLogger("bot.main")

# 2. 定義要載入的 Cog 模組清單 (後續步驟會逐一實現)
INITIAL_EXTENSIONS = [
    'cogs.admin',
    'cogs.cleaning',
    'cogs.finance',
    'cogs.shopping'
]

class RoommateBot(commands.Bot):
    def __init__(self):
        # 僅使用預設 Intents，不開啟敏感的 message_content
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.scheduler = None

    async def setup_hook(self):
        # ① 初始化全域單一資料庫連線池 (INV-3)
        await db.init_pool(config.DATABASE_URL)

        # ② 背景啟動 Web Server，綁定在同一個 event loop 中，嚴禁阻塞 Bot 登入
        self.loop.create_task(self.start_web_server())

        # ③ 啟動 APScheduler 機時排程器 (INV-3, INV-4)
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Taipei"))
        self.scheduler.add_job(self.global_cleaning_loop, 'interval', minutes=13)
        self.scheduler.start()
        logger.info("APScheduler 排程引擎已成功啟動。")

        # ④ 註冊持久化 UI (Persistent Views) 
        try:
            from cogs.shopping import PurchaseButton
            unpurchased = await db.fetch("SELECT id, item_name FROM shopping_items WHERE is_purchased = false")
            
            # 【多租戶資源防護】 Discord 限制單一 View 只能容納 25 個元件。
            # 當多個伺服器累積未買清單超過 25 項時，必須切塊 (chunk) 分批註冊，否則 bot.add_view 會崩潰。
            chunk_size = 25
            for i in range(0, len(unpurchased), chunk_size):
                chunk = unpurchased[i:i + chunk_size]
                view = discord.ui.View(timeout=None)
                for row in chunk:
                    view.add_item(PurchaseButton(str(row['id']), row['item_name']))
                self.add_view(view)
                
            logger.info(f"已成功重新註冊 {len(unpurchased)} 個公用品採購按鈕。")
        except Exception as e:
            logger.error(f"註冊採購按鈕失敗: {e}")

        # ⑤ 批次載入 Cog 擴充模組
        for ext in INITIAL_EXTENSIONS:
            await self.load_extension(ext)
            logger.info(f"擴充模組 {ext} 載入成功。")

    async def start_web_server(self):
        """建置輕量 aiohttp Web Server (免費資源策略)"""
        app = web.Application()
        app.router.add_get('/', self.handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', config.PORT)
        await site.start()
        logger.info(f"Web Server 已成功監聽連接埠: {config.PORT}")

    async def handle_ping(self, request):
        """/ 端點防禦：只做輕量 DB 探活與計數，絕不執行複雜商業邏輯"""
        try:
            await db.fetchval("SELECT 1")
            active_count = await db.fetchval("SELECT COUNT(*) FROM guilds WHERE is_active=true")
            return web.json_response({
                "status": "healthy",
                "active_guilds": active_count
            })
        except Exception as e:
            logger.error(f"Web Server Ping 探活失敗: {e}")
            return web.json_response({"status": "unhealthy", "error": str(e)}, status=500)

    async def global_cleaning_loop(self):
        """跨伺服器排程核心迴圈 (INV-4)"""
        try:
            active_guilds = await db.fetch("SELECT guild_id FROM guilds WHERE is_active=true")
        except Exception as e:
            logger.error(f"排程讀取啟用中伺服器失敗: {e}")
            return

        for row in active_guilds:
            gid = row['guild_id']
            try:
                # [修正] 觸發自訂事件，讓 Step 5 的 CleaningCog 能獨立監聽並執行冪等換班，達成模組解耦
                self.dispatch("cleaning_cycle", gid)
            except Exception as ex:
                logger.error(f"伺服器 {gid} 觸發定時換班失敗: {ex}", extra={"guild_id": gid})

bot = RoommateBot()

# 3. 多租戶版全域 Check (INV-1)
@bot.tree.interaction_check
async def global_interaction_check(interaction: discord.Interaction) -> bool:
    # Autocomplete 必須放行防卡死
    if interaction.type == discord.InteractionType.autocomplete:
        return True

    # 全域 Admin 最高權限放行
    if interaction.user.id in config.ADMIN_USER_IDS:
        return True

    if not interaction.guild_id:
        await interaction.response.send_message("❌ 此機器人只能在伺服器（群組）內使用。", ephemeral=True)
        return False

    # [修正] 使用 interaction.permissions 防禦 AttributeError，略過雞生蛋問題
    if interaction.permissions.manage_guild:
        return True

    # 檢查是否為該租戶註冊的有效室友
    is_roommate = await db.fetchval(
        "SELECT 1 FROM roommates WHERE guild_id = $1 AND user_id = $2",
        interaction.guild_id, interaction.user.id
    )
    if is_roommate:
        return True

    await interaction.response.send_message("❌ 您不是此伺服器登記的室友，無權使用此機器人指令！", ephemeral=True)
    return False

# 4. 全域錯誤處理攔截器
@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = interaction.guild_id or 0
    logger.error(f"指令執行異常: {error}", exc_info=error, extra={"guild_id": guild_id})

    err_msg = "❌ 執行指令時發生未預期的系統錯誤，請聯絡管理員。"
    if isinstance(error, app_commands.CheckFailure):
        # 若在 check 階段已回覆過，直接 return 避免雙重回覆引發崩潰
        if interaction.response.is_done():
            return
        err_msg = "❌ 您沒有權限執行此指令，或者您尚未被加入為本群組的登記室友。"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(err_msg, ephemeral=True)
        else:
            await interaction.response.send_message(err_msg, ephemeral=True)
    except Exception as e:
        logger.error(f"發送錯誤回饋失敗: {e}", extra={"guild_id": guild_id})

# 5. 租戶註冊與離開事件 (多租戶容量防護)
@bot.event
async def on_guild_join(guild: discord.Guild):
    guild_id = guild.id
    logger.info(f"機器人加入了新伺服器: {guild.name}", extra={"guild_id": guild_id})

    if guild_id != config.HOME_GUILD_ID:
        active_count = await db.fetchval(
            "SELECT COUNT(*) FROM guilds WHERE is_active=true AND guild_id != $1",
            config.HOME_GUILD_ID
        )
        if active_count >= config.MAX_GUILDS:
            logger.warning(f"外部租戶數量已達上限 ({config.MAX_GUILDS})。主動退出伺服器。", extra={"guild_id": guild_id})
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    await channel.send("👋 您好！本機器人的免費資源負載已達上限，無法加入新伺服器。如有需求請洽開發者。")
                    break
            await guild.leave()
            return

    await db.execute(
        """
        INSERT INTO guilds (guild_id, is_active, joined_at)
        VALUES ($1, true, NOW())
        ON CONFLICT (guild_id)
        DO UPDATE SET is_active = true
        """,
        guild_id
    )
    logger.info(f"租戶伺服器 {guild_id} 已成功註冊並啟用。", extra={"guild_id": guild_id})

    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"已完成伺服器 {guild_id} 專屬指令同步。", extra={"guild_id": guild_id})
    except Exception as e:
        logger.error(f"專屬指令同步失敗: {e}", extra={"guild_id": guild_id})

@bot.event
async def on_guild_remove(guild: discord.Guild):
    guild_id = guild.id
    logger.info(f"機器人已離開伺服器: {guild.name}", extra={"guild_id": guild_id})
    # 軟刪除 (INV-5)
    await db.execute("UPDATE guilds SET is_active = false WHERE guild_id = $1", guild_id)

@bot.event
async def on_ready():
    logger.info(f"機器人上線成功！登入身分: {bot.user}")
    try:
        await bot.tree.sync()
        logger.info("全域 Slash Commands 同步完成。")
    except Exception as e:
        logger.error(f"全域指令同步失敗: {e}")

    # [修正] 補上冷啟動防護！喚醒時必須立刻執行一次冪等換班檢查 (INV-4)
    logger.info("執行冷啟動進度補償：觸發全域輪值檢查...")
    await bot.global_cleaning_loop()

if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        print("錯誤: 找不到 DISCORD_TOKEN，請檢查 .env 檔案設定！")
    else:
        bot.run(config.DISCORD_TOKEN)
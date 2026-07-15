import os
import logging
import asyncio
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands
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
for handler in logging.root.handlers:
    handler.addFilter(GuildIDFilter())

logger = logging.getLogger("bot.main")

# 2. 機器人初始化
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 3. 建立 Web 伺服器 (供 Render 與 cron-job 喚醒用)
async def health_check(request):
    return web.json_response({"status": "healthy"})

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info("Web 伺服器已啟動，監聽 Render 喚醒請求")

scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Taipei"))

# 4. 核心事件處理
@bot.event
async def setup_hook():
    # ✅ 已修正：使用正確的資料庫連線函數與設定檔
    await db.init_pool(config.DATABASE_URL)
    
    # ✅ 已修正：載入名稱與你實際資料夾內完全相符的模組
    await bot.load_extension("cogs.admin")
    await bot.load_extension("cogs.cleaning")
    await bot.load_extension("cogs.finance")
    await bot.load_extension("cogs.shopping")
    
    # 啟動背景 Web 伺服器
    bot.loop.create_task(start_web_server())

@bot.event
async def on_ready():
    logger.info(f"機器人已登入為: {bot.user}")
    
    # 👇👇👇 跑過一次後需要刪除的區塊開始 👇👇👇
    logger.info("開始清理伺服器舊快取與同步全域指令...")
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    logger.info("✅ 全域指令同步完成！(解決指令重複問題，下次啟動前請將這段程式碼刪除)")
    # 👆👆👆 跑過一次後需要刪除的區塊結束 👆👆👆

    if not scheduler.running:
        scheduler.start()

@bot.event
async def on_guild_join(guild):
    logger.info(f"機器人加入了新伺服器: {guild.name}", extra={"guild_id": guild.id})
    
    # 租戶數量防護邏輯
    if guild.id == config.HOME_GUILD_ID:
        return
        
    active_count = await db.fetchval("SELECT COUNT(*) FROM guilds WHERE is_active = true AND guild_id != $1", config.HOME_GUILD_ID)
    
    if active_count >= config.MAX_GUILDS:
        logger.warning(f"已達伺服器數量上限 ({config.MAX_GUILDS})，自動退出。", extra={"guild_id": guild.id})
        try:
            if guild.system_channel:
                await guild.system_channel.send("⚠️ 抱歉，本機器人目前的服務名額已滿，自動退出。")
        except:
            pass
        await guild.leave()
    else:
        await db.execute("INSERT INTO guilds (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild.id)

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)

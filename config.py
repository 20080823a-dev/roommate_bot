import os
from pathlib import Path
from dotenv import load_dotenv

# 確保讀取當前目錄下的 .env 檔案
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Discord 基礎設定
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# 資料庫與 Web Server 連接埠設定
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8080"))

# 多租戶防護與規模化管理核心變數 (INV-3)
# 解析全域管理員 ID 列表，將逗號分隔字串轉換為整數清單
admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = [int(uid.strip()) for uid in admin_ids_raw.split(",") if uid.strip().isdigit()]

# 主要自用伺服器 ID
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))

# 最大伺服器數量限制
MAX_GUILDS = int(os.getenv("MAX_GUILDS", "5"))

# 單一租戶最大成員數限制
MAX_ROOMMATES_PER_GUILD = int(os.getenv("MAX_ROOMMATES_PER_GUILD", "10"))
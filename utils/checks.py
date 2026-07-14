import discord
from discord import app_commands
import config

def is_admin():
    """
    雙層權限控管裝飾器 (功能需求一.2)
    允許「全域 Admin (ADMIN_USER_IDS)」或「該伺服器的管理員 (manage_guild)」執行。
    """
    def predicate(interaction: discord.Interaction) -> bool:
        # 1. 全域 Admin 身分，無條件放行
        if interaction.user.id in config.ADMIN_USER_IDS:
            return True
        # 2. 該伺服器管理者身分，放行
        if interaction.permissions.manage_guild:
            return True
        # 皆不符合，拋出例外由全域 error handler 攔截
        raise app_commands.CheckFailure("權限不足：您必須是「本伺服器管理員」或「全域維運人員」才能執行此指令。")
    
    return app_commands.check(predicate)
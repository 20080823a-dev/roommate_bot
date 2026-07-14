import asyncpg
import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger("bot.database")

class DatabaseManager:
    def __init__(self):
        self.pool = None
        self.dsn = None

    async def init_pool(self, dsn: str):
        self.dsn = dsn
        logger.info("Initializing database connection pool (Session Mode)...")
        # 建立共用連線池 (INV-3)
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=5,
            max_inactive_connection_lifetime=300, # 5分鐘閒置回收防呆
            ssl="require" 
        )
        logger.info("Database connection pool initialized.")

    async def _recreate_pool(self):
        logger.warning("Recreating database pool due to connection drops...")
        if self.pool:
            await self.pool.close()
        await self.init_pool(self.dsn)

    async def acquire(self):
        """取得連線，包含探活與斷線重連機制 (冷啟動防禦)"""
        max_retries = 3
        backoff = 1

        for attempt in range(max_retries):
            try:
                conn = await self.pool.acquire()
                # 探活 (Liveness check)
                await conn.execute("SELECT 1")
                return conn
            except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError) as e:
                logger.warning(f"Connection check failed (Attempt {attempt + 1}/{max_retries}): {e}")
                if 'conn' in locals() and conn:
                    await self.pool.release(conn)
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2 # 指數退避 (1s -> 2s -> 4s)
                else:
                    logger.error("All pool retries failed, attempting full pool recreation.")
                    await self._recreate_pool()
                    conn = await self.pool.acquire()
                    await conn.execute("SELECT 1")
                    return conn

    async def release(self, conn):
        """釋放連線回池中"""
        if self.pool and conn:
            await self.pool.release(conn)

    @asynccontextmanager
    async def transaction(self):
        """提供安全的 Transaction Context Manager，保證寫入安全 (INV-2)"""
        conn = await self.acquire()
        tr = conn.transaction()
        await tr.start()
        try:
            yield conn
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise
        finally:
            await self.release(conn)

    # 便捷的共用查詢方法
    async def fetch(self, query, *args):
        conn = await self.acquire()
        try:
            return await conn.fetch(query, *args)
        finally:
            await self.release(conn)

    async def fetchrow(self, query, *args):
        conn = await self.acquire()
        try:
            return await conn.fetchrow(query, *args)
        finally:
            await self.release(conn)

    async def fetchval(self, query, *args):
        conn = await self.acquire()
        try:
            return await conn.fetchval(query, *args)
        finally:
            await self.release(conn)

    async def execute(self, query, *args):
        conn = await self.acquire()
        try:
            return await conn.execute(query, *args)
        finally:
            await self.release(conn)

# 建立全域單一實例
db = DatabaseManager()
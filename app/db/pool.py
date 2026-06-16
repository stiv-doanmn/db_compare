"""Tạo và test asyncpg connection pool."""
from __future__ import annotations

import asyncpg

from ..config import POOL_MAX_SIZE, POOL_MIN_SIZE
from ..models import DSNConfig


async def create_pool(dsn: DSNConfig) -> asyncpg.Pool:
    """Tạo pool. Đặt timeout ngắn để test connection fail nhanh."""
    return await asyncpg.create_pool(
        host=dsn.host,
        port=dsn.port,
        database=dsn.dbname,
        user=dsn.user,
        password=dsn.password,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        command_timeout=60,
        timeout=10,
    )


async def test_connection(pool: asyncpg.Pool) -> str:
    """Trả về version string nếu OK, raise nếu fail."""
    async with pool.acquire() as conn:
        version = await conn.fetchval("SHOW server_version")
        return f"PostgreSQL {version}"


async def close_pool(pool: asyncpg.Pool | None) -> None:
    if pool is not None:
        try:
            await pool.close()
        except Exception:
            pass

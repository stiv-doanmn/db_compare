"""Phase 2 — ước lượng số row + gợi ý mode compare."""
from __future__ import annotations

import asyncpg

from ..config import LARGE_TABLE_THRESHOLD
from ..models import JobState, TableEstimate

# reltuples là estimate từ ANALYZE, không scan toàn bảng.
_RELTUPLES_QUERY = """
SELECT c.relname AS table_name, c.reltuples::bigint AS rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
"""

# Bảng có cột id (PK chuẩn Odoo) → mới value-compare được.
_HAS_ID_QUERY = """
SELECT table_name
FROM information_schema.columns
WHERE table_schema = 'public' AND column_name = 'id'
"""

# Throughput ước lượng cho việc quét + hash (rows/giây, gộp cả 2 DB).
_SCAN_THROUGHPUT = 80_000


async def _fetch_reltuples(pool: asyncpg.Pool) -> dict[str, int]:
    rows = await pool.fetch(_RELTUPLES_QUERY)
    return {r["table_name"]: max(0, r["rows"]) for r in rows}


async def _fetch_has_id(pool: asyncpg.Pool) -> set[str]:
    rows = await pool.fetch(_HAS_ID_QUERY)
    return {r["table_name"] for r in rows}


def suggest_mode(*, is_custom: bool, status: str, max_rows: int, has_id: bool) -> str:
    if not has_id:
        # m2m rel / bảng không id: so theo composite key. Lớn quá thì chỉ count.
        return "count-only" if max_rows > LARGE_TABLE_THRESHOLD else "full"
    if is_custom:
        return "full"  # bảng custom luôn full, kể cả khi lớn
    if max_rows > LARGE_TABLE_THRESHOLD:
        return "count-only"
    if status == "changed":
        return "full"
    return "count-only"  # base + identical → chỉ cần đối chiếu count


def estimate_time(max_rows: int, mode: str) -> float:
    """Giây ước lượng cho 1 bảng."""
    if mode == "count-only":
        return 0.5
    return 1.0 + max_rows / _SCAN_THROUGHPUT


async def build_estimates(job: JobState) -> None:
    """Điền job.estimates cho các bảng tồn tại ở CẢ 2 DB."""
    rel_a = await _fetch_reltuples(job.pool_a)
    rel_b = await _fetch_reltuples(job.pool_b)
    has_id_a = await _fetch_has_id(job.pool_a)
    has_id_b = await _fetch_has_id(job.pool_b)

    estimates: dict[str, TableEstimate] = {}
    for name, sd in job.schema_tables.items():
        if sd.status in ("new", "dropped"):
            continue  # chỉ compare bảng có ở cả 2
        rows_a = rel_a.get(name, 0)
        rows_b = rel_b.get(name, 0)
        has_id = name in has_id_a and name in has_id_b
        estimates[name] = TableEstimate(
            name=name,
            rows_a=rows_a,
            rows_b=rows_b,
            has_id=has_id,
            suggested_mode=suggest_mode(
                is_custom=sd.is_custom,
                status=sd.status,
                max_rows=max(rows_a, rows_b),
                has_id=has_id,
            ),
        )
    job.estimates = estimates

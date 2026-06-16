"""Phase 1 — so sánh schema qua information_schema."""
from __future__ import annotations

import asyncpg

from ..models import ColumnDiff, JobState, TableSchemaDiff

_COLUMNS_QUERY = """
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""


async def _fetch_columns(pool: asyncpg.Pool) -> dict[str, dict[str, dict]]:
    """{table: {column: {data_type, is_nullable, column_default}}}"""
    rows = await pool.fetch(_COLUMNS_QUERY)
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["table_name"], {})[r["column_name"]] = {
            "data_type": r["data_type"],
            "is_nullable": r["is_nullable"],
            "column_default": r["column_default"],
        }
    return out


def _diff_columns(cols_a: dict, cols_b: dict) -> list[ColumnDiff]:
    diffs: list[ColumnDiff] = []
    set_a, set_b = set(cols_a), set(cols_b)

    for name in sorted(set_b - set_a):  # có ở B (v19), thiếu ở A (v17)
        diffs.append(ColumnDiff(name=name, kind="added", detail_b=cols_b[name]["data_type"]))
    for name in sorted(set_a - set_b):  # có ở A, mất ở B
        diffs.append(ColumnDiff(name=name, kind="dropped", detail_a=cols_a[name]["data_type"]))

    for name in sorted(set_a & set_b):
        a, b = cols_a[name], cols_b[name]
        if a["data_type"] != b["data_type"]:
            diffs.append(
                ColumnDiff(name, "type_changed", a["data_type"], b["data_type"])
            )
        if a["is_nullable"] != b["is_nullable"]:
            diffs.append(
                ColumnDiff(name, "nullable_changed", a["is_nullable"], b["is_nullable"])
            )
        if a["column_default"] != b["column_default"]:
            diffs.append(
                ColumnDiff(
                    name,
                    "default_changed",
                    str(a["column_default"]),
                    str(b["column_default"]),
                )
            )
    return diffs


async def run_schema_diff(job: JobState) -> None:
    """Điền job.schema_tables. A = v17 (source), B = v19 (target)."""
    cols_a = await _fetch_columns(job.pool_a)
    cols_b = await _fetch_columns(job.pool_b)

    tables_a, tables_b = set(cols_a), set(cols_b)
    result: dict[str, TableSchemaDiff] = {}

    for name in sorted(tables_b - tables_a):  # mới xuất hiện ở v19
        result[name] = TableSchemaDiff(
            name=name, status="new", is_custom=job.is_custom(name)
        )
    for name in sorted(tables_a - tables_b):  # bị bỏ ở v19
        result[name] = TableSchemaDiff(
            name=name, status="dropped", is_custom=job.is_custom(name)
        )
    for name in sorted(tables_a & tables_b):
        diffs = _diff_columns(cols_a[name], cols_b[name])
        result[name] = TableSchemaDiff(
            name=name,
            status="changed" if diffs else "identical",
            is_custom=job.is_custom(name),
            columns=diffs,
        )

    job.schema_tables = result

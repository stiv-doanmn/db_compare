"""Phase 2 (mở rộng) — so sánh constraint giữa 2 DB.

So các loại: PRIMARY KEY (p), UNIQUE (u), FOREIGN KEY (f), CHECK (c).
Dùng ``pg_get_constraintdef`` để lấy định nghĩa canonical (text), so theo
``(table, conname)``. Index thường (không phải constraint) KHÔNG xét ở đây.

Phát hiện: constraint chỉ có ở A / chỉ có ở B / cùng tên nhưng định nghĩa khác.
Đây là metadata (pg_catalog) — query rẻ, không scan row.
"""
from __future__ import annotations

import asyncpg

from ..models import ConstraintDiff, JobState

_CONSTRAINTS_QUERY = """
SELECT rel.relname     AS table_name,
       con.conname     AS name,
       con.contype     AS contype,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel       ON rel.oid = con.conrelid
JOIN pg_namespace nsp   ON nsp.oid = rel.relnamespace
WHERE nsp.nspname = 'public'
  AND con.contype IN ('p', 'u', 'f', 'c')
ORDER BY rel.relname, con.conname
"""


async def _fetch_constraints(pool: asyncpg.Pool) -> dict[str, dict[str, dict]]:
    """{table: {conname: {"contype": .., "definition": ..}}}"""
    rows = await pool.fetch(_CONSTRAINTS_QUERY)
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["table_name"], {})[r["name"]] = {
            "contype": r["contype"],
            "definition": r["definition"],
        }
    return out


def _diff_table_constraints(cons_a: dict, cons_b: dict) -> list[ConstraintDiff]:
    diffs: list[ConstraintDiff] = []
    names_a, names_b = set(cons_a), set(cons_b)

    for name in sorted(names_b - names_a):  # mới ở B (v19)
        b = cons_b[name]
        diffs.append(ConstraintDiff(name, b["contype"], "added", def_b=b["definition"]))
    for name in sorted(names_a - names_b):  # bị bỏ ở B
        a = cons_a[name]
        diffs.append(ConstraintDiff(name, a["contype"], "dropped", def_a=a["definition"]))
    for name in sorted(names_a & names_b):
        a, b = cons_a[name], cons_b[name]
        if a["definition"] != b["definition"]:
            diffs.append(
                ConstraintDiff(
                    name, b["contype"], "changed",
                    def_a=a["definition"], def_b=b["definition"],
                )
            )
    return diffs


async def run_constraint_diff(job: JobState) -> None:
    """Gắn constraint diff vào job.schema_tables. Chạy SAU run_schema_diff.

    Chỉ xét bảng có ở CẢ 2 DB (status changed/identical). Nếu bảng đang
    'identical' (cột giống nhau) mà constraint lệch → nâng lên 'changed'.
    """
    cons_a = await _fetch_constraints(job.pool_a)
    cons_b = await _fetch_constraints(job.pool_b)

    for name, td in job.schema_tables.items():
        if td.status in ("new", "dropped"):
            continue  # bảng chỉ tồn tại 1 bên — không so constraint
        td.constraints = _diff_table_constraints(
            cons_a.get(name, {}), cons_b.get(name, {})
        )
        if td.constraints and td.status == "identical":
            td.status = "changed"

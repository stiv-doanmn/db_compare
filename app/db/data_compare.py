"""Phase 3 — so sánh dữ liệu giữa 2 DB.

Chiến lược:
- count-only: chỉ COUNT(*) hai bên → Δ rows.
- full / intersection: COUNT(*) + merge-join theo id trên row-hash (MD5).
  Một lượt quét sắp xếp theo id phát hiện đồng thời: id chỉ có ở A, id chỉ có
  ở B, và id chung nhưng giá trị khác (hash khác).
- full      = hash trên toàn bộ cột chung (báo warning nếu schema lệch).
- intersection = hash trên các cột tồn tại ở cả 2 DB.
"""
from __future__ import annotations

import asyncio
import time

import asyncpg

from ..config import BATCH_SIZE, MAX_SAMPLES
from ..models import JobState, TableProgress

_PROGRESS_TICK = 5000  # số row giữa mỗi lần đẩy cập nhật progress


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _table_columns(pool: asyncpg.Pool, table: str) -> list[str]:
    rows = await pool.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        """,
        table,
    )
    return [r["column_name"] for r in rows]


async def _count(pool: asyncpg.Pool, table: str) -> int:
    return await pool.fetchval(f"SELECT count(*) FROM {_qident(table)}")


async def _iter_rows(
    pool: asyncpg.Pool, table: str, key_cols: list[str], hash_cols: list[str]
):
    """Yield (key_tuple, hash) sắp xếp theo key_cols, keyset-paginate theo tuple key.

    - key_cols = ["id"] cho bảng thường.
    - key_cols = toàn bộ cột chung cho bảng không có id (m2m rel) — cả dòng là key.
    """
    key_sel = ", ".join(_qident(c) for c in key_cols)
    if hash_cols:
        row_expr = "ROW(" + ", ".join(_qident(c) for c in hash_cols) + ")::text"
        hash_expr = f"md5({row_expr})"
    else:
        hash_expr = "''"  # không có cột value để hash → chỉ so sự tồn tại của key

    base = f"SELECT {key_sel}, {hash_expr} AS __h FROM {_qident(table)}"
    n = len(key_cols)
    lhs = "(" + key_sel + ")"
    rhs = "(" + ", ".join(f"${i + 1}" for i in range(n)) + ")"
    sql_first = f"{base} ORDER BY {key_sel} LIMIT $1"
    sql_next = f"{base} WHERE {lhs} > {rhs} ORDER BY {key_sel} LIMIT ${n + 1}"

    async with pool.acquire() as conn:
        last_key = None
        while True:
            if last_key is None:
                rows = await conn.fetch(sql_first, BATCH_SIZE)
            else:
                rows = await conn.fetch(sql_next, *last_key, BATCH_SIZE)
            if not rows:
                break
            for r in rows:
                yield tuple(r[c] for c in key_cols), r["__h"]
            last_key = tuple(rows[-1][c] for c in key_cols)
            if len(rows) < BATCH_SIZE:
                break


async def _compare_one(job: JobState, table: str) -> None:
    prog = job.progress[table]
    prog.status = "running"
    prog.started_at = time.time()
    await _notify(job)

    try:
        prog.count_a = await _count(job.pool_a, table)
        prog.count_b = await _count(job.pool_b, table)

        if prog.mode == "count-only":
            prog.status = "warning" if prog.count_delta != 0 else "done"
            return

        cols_a = await _table_columns(job.pool_a, table)
        cols_b = await _table_columns(job.pool_b, table)
        common = [c for c in cols_a if c in set(cols_b)]

        if not common:
            prog.note = "Không có cột chung → chỉ so count"
            prog.status = "warning" if prog.count_delta != 0 else "done"
            return

        notes: list[str] = []
        if "id" in common:
            key_cols = ["id"]
            hash_cols = [c for c in common if c != "id"]
        else:
            # m2m rel / bảng không id → composite key = toàn bộ cột chung
            key_cols = list(common)
            hash_cols = []
            notes.append("Không có id — so theo composite key (toàn bộ cột chung)")

        if prog.mode == "full":
            only_a_cols = [c for c in cols_a if c not in set(cols_b)]
            only_b_cols = [c for c in cols_b if c not in set(cols_a)]
            if only_a_cols or only_b_cols:
                notes.append(
                    f"Schema lệch — bỏ qua cột chỉ có ở 1 bên "
                    f"(A: {len(only_a_cols)}, B: {len(only_b_cols)})"
                )
        prog.note = " · ".join(notes)
        prog.column_scope = hash_cols if hash_cols else key_cols

        await _merge_join(job, prog, table, key_cols, hash_cols)

        # Bảng có id + có cột value: fetch giá trị thật để biết cột nào khác.
        if key_cols == ["id"] and prog.sample_mismatch and hash_cols:
            await _fetch_mismatch_details(job, prog, table, hash_cols)

        has_diff = prog.only_in_a or prog.only_in_b or prog.value_mismatch or prog.count_delta
        prog.status = "warning" if has_diff else "done"
    except Exception as exc:  # noqa: BLE001
        prog.status = "error"
        prog.error = f"{type(exc).__name__}: {exc}"
    finally:
        prog.finished_at = time.time()
        await _notify(job)


def _keyval(key: tuple):
    """Hiển thị key: scalar nếu 1 cột, ngược lại tuple (vd m2m: (order_id, tag_id))."""
    return key[0] if len(key) == 1 else tuple(key)


async def _merge_join(
    job: JobState,
    prog: TableProgress,
    table: str,
    key_cols: list[str],
    hash_cols: list[str],
) -> None:
    prog.total = max(prog.count_a, prog.count_b)

    ia = _iter_rows(job.pool_a, table, key_cols, hash_cols)
    ib = _iter_rows(job.pool_b, table, key_cols, hash_cols)
    a = await anext(ia, None)
    b = await anext(ib, None)

    since_tick = 0
    while a is not None and b is not None:
        ka, ha = a
        kb, hb = b
        if ka < kb:
            prog.only_in_a += 1
            if len(prog.sample_only_a) < MAX_SAMPLES:
                prog.sample_only_a.append(_keyval(ka))
            a = await anext(ia, None)
        elif ka > kb:
            prog.only_in_b += 1
            if len(prog.sample_only_b) < MAX_SAMPLES:
                prog.sample_only_b.append(_keyval(kb))
            b = await anext(ib, None)
        else:
            if ha != hb:
                prog.value_mismatch += 1
                if len(prog.sample_mismatch) < MAX_SAMPLES:
                    prog.sample_mismatch.append(_keyval(ka))
            a = await anext(ia, None)
            b = await anext(ib, None)
        prog.scanned += 1
        since_tick += 1
        if since_tick >= _PROGRESS_TICK:
            since_tick = 0
            await _notify(job)

    while a is not None:
        prog.only_in_a += 1
        if len(prog.sample_only_a) < MAX_SAMPLES:
            prog.sample_only_a.append(_keyval(a[0]))
        prog.scanned += 1
        a = await anext(ia, None)
    while b is not None:
        prog.only_in_b += 1
        if len(prog.sample_only_b) < MAX_SAMPLES:
            prog.sample_only_b.append(_keyval(b[0]))
        prog.scanned += 1
        b = await anext(ib, None)


def _fmt_val(v) -> str:
    if v is None:
        return "∅"
    s = str(v)
    return s if len(s) <= 120 else s[:117] + "…"


async def _fetch_mismatch_details(
    job: JobState, prog: TableProgress, table: str, hash_cols: list[str]
) -> None:
    """Fetch giá trị thật của các id mismatch (mẫu) ở 2 DB, so cột để biết cột nào khác."""
    ids = list(prog.sample_mismatch)
    col_list = ", ".join(_qident(c) for c in (["id"] + hash_cols))
    sql = f"SELECT {col_list} FROM {_qident(table)} WHERE id = ANY($1::bigint[])"

    rows_a = await job.pool_a.fetch(sql, ids)
    rows_b = await job.pool_b.fetch(sql, ids)
    map_a = {r["id"]: r for r in rows_a}
    map_b = {r["id"]: r for r in rows_b}

    details: list[dict] = []
    for rid in ids:
        ra, rb = map_a.get(rid), map_b.get(rid)
        if ra is None or rb is None:
            continue
        diffs = []
        for c in hash_cols:
            va, vb = ra[c], rb[c]
            if va != vb:
                diffs.append({"col": c, "a": _fmt_val(va), "b": _fmt_val(vb)})
        if diffs:
            details.append({"id": rid, "diffs": diffs})
    prog.mismatch_details = details


async def _notify(job: JobState) -> None:
    """Đánh thức SSE stream để render lại trạng thái hiện tại."""
    try:
        job.queue.put_nowait("tick")
    except asyncio.QueueFull:
        pass


async def run_compare(job: JobState) -> None:
    """Chạy toàn bộ bảng đã chọn với giới hạn song song qua semaphore."""
    from ..config import MAX_WORKERS

    job.status = "running"
    job.compare_started_at = time.time()
    job.compare_finished_at = None
    sem = asyncio.Semaphore(MAX_WORKERS)

    async def _guarded(table: str) -> None:
        async with sem:
            await _compare_one(job, table)

    try:
        await asyncio.gather(*(_guarded(t) for t in job.progress))
    finally:
        job.status = "done"
        job.compare_finished_at = time.time()
        await _notify(job)

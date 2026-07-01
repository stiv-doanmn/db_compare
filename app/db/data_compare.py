"""Phase 3 — so sánh dữ liệu giữa 2 DB.

Chiến lược:
- count-only: chỉ COUNT(*) hai bên → Δ rows.
- full: COUNT(*) + merge-join theo id trên row-hash (MD5) của TẤT CẢ cột chung.
  Một lượt quét sắp xếp theo id phát hiện đồng thời: id chỉ có ở A, id chỉ có
  ở B, và id chung nhưng giá trị khác (hash khác). Cột chỉ có ở 1 bên không so
  được giá trị → báo ở schema diff.

Chuẩn hoá trước khi hash để giảm false positive khi so 2 phiên bản PostgreSQL
khác nhau (vd v17/PG14 ↔ v19/PG16): DateStyle/TimeZone/extra_float_digits set
giống nhau ở pool (xem db/pool.py), numeric bỏ trailing-zero, json → jsonb.
"""
from __future__ import annotations

import asyncio
import time

import asyncpg

from .. import spill
from ..config import (
    BATCH_SIZE,
    KEYWORD_SAMPLE_IDS,
    MAX_MISMATCH_DETAIL,
    MISMATCH_DETAIL_BATCH,
    PREVIEW_SAMPLES,
)
from ..models import JobState, KeywordHit, TableProgress
from ..store import clear_checkpoint, load_checkpoint, save_checkpoint

_PROGRESS_TICK = 5000  # số row giữa mỗi lần đẩy cập nhật progress
_CHECKPOINT_EVERY = 5  # lưu checkpoint xuống file mỗi N tick (~25k row)


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fmt_cols(cols: list[str], limit: int = 10) -> str:
    """Liệt kê tên cột, cắt bớt nếu quá dài: 'a, b, c … (+5 cột)'."""
    if len(cols) <= limit:
        return ", ".join(cols)
    return ", ".join(cols[:limit]) + f" … (+{len(cols) - limit} cột)"


async def _column_types(pool: asyncpg.Pool, table: str) -> dict[str, str]:
    """{column_name: data_type} theo ordinal_position (dict giữ thứ tự chèn)."""
    rows = await pool.fetch(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        """,
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


# Chuẩn hoá numeric: bỏ trailing-zero do khác scale (1.50 ↔ 1.5, 2.00 ↔ 2).
# Dùng regexp thay vì trim_scale() để chạy được trên mọi bản PostgreSQL.
_NUM_NORM = r"regexp_replace(regexp_replace({q}::text, '(\.\d*?)0+$', '\1'), '\.$', '')"


def _norm_expr(col: str, dtype: str) -> str:
    """Biểu thức SQL trả text đã chuẩn hoá cho 1 cột (để đưa vào row-hash)."""
    q = _qident(col)
    if dtype == "numeric":
        return _NUM_NORM.format(q=q)
    if dtype == "json":
        # json (không phải jsonb) → ép jsonb để chuẩn hoá thứ tự key / whitespace.
        return f"{q}::jsonb::text"
    return f"{q}::text"


async def _count(pool: asyncpg.Pool, table: str) -> int:
    return await pool.fetchval(f"SELECT count(*) FROM {_qident(table)}")


async def _iter_rows(
    pool: asyncpg.Pool,
    table: str,
    key_cols: list[str],
    hash_cols: list[str],
    start_after: tuple | None = None,
    col_types: dict[str, str] | None = None,
):
    """Yield (key_tuple, hash) sắp xếp theo key_cols, keyset-paginate theo tuple key.

    - key_cols = ["id"] cho bảng thường.
    - key_cols = toàn bộ cột chung cho bảng không có id (m2m rel) — cả dòng là key.
    - start_after: nếu có, chỉ quét các key > start_after (dùng để resume checkpoint).
    - col_types: {cột: data_type} của chính DB này → chuẩn hoá giá trị trước khi hash.
    """
    types = col_types or {}
    key_sel = ", ".join(_qident(c) for c in key_cols)
    if hash_cols:
        # Hash trên text đã chuẩn hoá từng cột (numeric/json...), bọc trong ROW()
        # để giữ phân tách & xử lý NULL nhất quán.
        row_expr = "ROW(" + ", ".join(_norm_expr(c, types.get(c, "")) for c in hash_cols) + ")::text"
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
        # Resume: coi start_after như "last_key" của lần quét trước.
        last_key = tuple(start_after) if start_after is not None else None
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


def _ckpt_payload(prog: TableProgress, spill_off: dict | None = None) -> dict:
    """Snapshot tiến độ 1 bảng để ghi xuống file store (phục vụ resume).

    spill_off: offset byte của các spill file tại đúng thời điểm snapshot, để khi
    resume cắt spill về vị trí khớp resume_after_id (tránh ghi trùng).
    Các sample_* chỉ là PREVIEW (≤ PREVIEW_SAMPLES) — dữ liệu full nằm ở spill.
    """
    return {
        "mode": prog.mode,
        "status": prog.status,
        "resume_after_id": prog.resume_after_id,
        "scanned": prog.scanned,
        "count_a": prog.count_a,
        "count_b": prog.count_b,
        "only_in_a": prog.only_in_a,
        "only_in_b": prog.only_in_b,
        "value_mismatch": prog.value_mismatch,
        "sample_only_a": list(prog.sample_only_a),
        "sample_only_b": list(prog.sample_only_b),
        "sample_mismatch": list(prog.sample_mismatch),
        "spill_off": spill_off,
        "error": prog.error,
    }


def _restore_from_checkpoint(prog: TableProgress, cp: dict) -> tuple:
    """Nạp lại counters/samples từ checkpoint, trả về start_after để quét tiếp."""
    prog.resume_after_id = cp["resume_after_id"]
    prog.scanned = cp.get("scanned", 0)
    # count_a/count_b giữ giá trị mới đếm (chính xác hơn checkpoint cũ).
    prog.only_in_a = cp.get("only_in_a", 0)
    prog.only_in_b = cp.get("only_in_b", 0)
    prog.value_mismatch = cp.get("value_mismatch", 0)
    prog.sample_only_a = list(cp.get("sample_only_a") or [])
    prog.sample_only_b = list(cp.get("sample_only_b") or [])
    prog.sample_mismatch = list(cp.get("sample_mismatch") or [])
    prog.resumed = True
    return (cp["resume_after_id"],)


async def _compare_one(job: JobState, table: str) -> None:
    prog = job.progress[table]
    pair_key = job.pair_key()
    prog.status = "running"
    prog.started_at = time.time()
    await _notify(job)

    try:
        prog.count_a = await _count(job.pool_a, table)
        prog.count_b = await _count(job.pool_b, table)

        if prog.mode == "count-only":
            # count-only: chỉ đối chiếu số lượng. Lệch count → "Khác biệt".
            prog.status = "warning" if prog.count_delta != 0 else "done"
            clear_checkpoint(pair_key, table)
            return

        types_a = await _column_types(job.pool_a, table)
        types_b = await _column_types(job.pool_b, table)
        cols_a, cols_b = list(types_a), list(types_b)
        common = [c for c in cols_a if c in set(cols_b)]

        if not common:
            prog.note = "Không có cột chung → chỉ so count"
            prog.status = "done"
            clear_checkpoint(pair_key, table)
            return

        notes: list[str] = []
        if "id" in common:
            key_cols = ["id"]
            hash_cols = [c for c in common if c != "id"]
        else:
            # m2m rel / bảng không id → composite key = toàn bộ cột chung
            key_cols = list(common)
            hash_cols = []
            notes.append(
                f"Bảng không có cột id — khớp theo composite key gồm "
                f"{len(key_cols)} cột: {_fmt_cols(key_cols)}"
            )

        # Schema lệch: cột chỉ có ở 1 bên sẽ KHÔNG được đưa vào hash so sánh.
        only_a_cols = [c for c in cols_a if c not in set(cols_b)]
        only_b_cols = [c for c in cols_b if c not in set(cols_a)]
        if only_a_cols or only_b_cols:
            parts = []
            if only_a_cols:
                parts.append(f"chỉ có ở {job.label_a}: {_fmt_cols(only_a_cols)}")
            if only_b_cols:
                parts.append(f"chỉ có ở {job.label_b}: {_fmt_cols(only_b_cols)}")
            notes.append("Schema lệch, bỏ qua khi so giá trị — " + " · ".join(parts))

        # Resume checkpoint — chỉ áp dụng cho bảng có id (sort id tăng dần) VÀ
        # checkpoint có spill_off (để cắt spill khớp, tránh ghi trùng).
        start_after: tuple | None = None
        if key_cols == ["id"]:
            cp = load_checkpoint(pair_key, table)
            if (
                cp
                and cp.get("mode") == prog.mode
                and cp.get("resume_after_id") is not None
                and cp.get("status") in ("error", "running")
                and cp.get("spill_off")
            ):
                start_after = _restore_from_checkpoint(prog, cp)
                for kind, sz in cp["spill_off"].items():  # cắt spill về điểm checkpoint
                    spill.truncate(pair_key, table, kind, sz)
                notes.append(f"Resume từ id > {start_after[0]}")

        prog.note = " · ".join(notes)
        prog.column_scope = hash_cols if hash_cols else key_cols

        await _merge_join(
            job, prog, table, key_cols, hash_cols, start_after, pair_key,
            types_a, types_b,
        )

        # Bảng có id + có cột value + có mismatch: fetch giá trị thật để biết cột
        # nào khác — stream toàn bộ từ spill, ghi chi tiết ra spill (không cap RAM).
        if key_cols == ["id"] and hash_cols and prog.value_mismatch:
            await _fetch_mismatch_details(job, prog, table, hash_cols, pair_key)

        # Chỉ warning khi THẬT SỰ có khác biệt: dòng chỉ-ở-1-bên, giá trị khác,
        # hoặc thay đổi cột (thêm/bỏ/đổi kiểu). Việc có id hay không, hay lệch
        # count đơn thuần — KHÔNG coi là warning.
        sd = job.schema_tables.get(table)
        col_changed = bool(sd and (sd.added or sd.dropped or sd.type_changed))
        has_diff = (
            prog.only_in_a or prog.only_in_b or prog.value_mismatch or col_changed
        )
        prog.status = "warning" if has_diff else "done"
        clear_checkpoint(pair_key, table)  # quét xong → bỏ checkpoint
    except Exception as exc:  # noqa: BLE001
        prog.status = "error"
        prog.error = f"{type(exc).__name__}: {exc}"
        # KHÔNG ghi đè checkpoint ở đây: checkpoint 'running' định kỳ (kèm
        # spill_off) vẫn nằm trên đĩa và đã khớp spill → lần sau resume an toàn.
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
    start_after: tuple | None = None,
    pair_key: str | None = None,
    types_a: dict[str, str] | None = None,
    types_b: dict[str, str] | None = None,
) -> None:
    prog.total = max(prog.count_a, prog.count_b)
    # Chỉ checkpoint được khi key là id đơn (số) — resume bằng id > X.
    checkpointable = key_cols == ["id"] and pair_key is not None

    # Chạy mới (không resume) → xoá spill cũ của bảng này.
    if pair_key is not None and start_after is None:
        spill.reset(pair_key, table)

    def _mark(consumed_key: tuple) -> None:
        """Ghi nhận id nhỏ nhất vừa xử lý xong cả 2 bên làm điểm resume."""
        if checkpointable:
            prog.resume_after_id = consumed_key[0]

    # Spill append handle — ghi TOÀN BỘ findings ra đĩa, RAM chỉ giữ preview.
    fa = spill.open_append(pair_key, table, "only_a") if pair_key else None
    fb = spill.open_append(pair_key, table, "only_b") if pair_key else None
    fm = spill.open_append(pair_key, table, "mismatch") if pair_key else None

    def _emit(fh, lst, key) -> None:
        kv = _keyval(key)
        if fh is not None:
            spill.write(fh, kv)
        if len(lst) < PREVIEW_SAMPLES:
            lst.append(kv)

    def _save_ckpt() -> None:
        # Flush spill rồi chốt offset → checkpoint khớp spill khi resume.
        off = None
        if fa is not None and fb is not None and fm is not None:
            fa.flush(); fb.flush(); fm.flush()
            off = {"only_a": fa.tell(), "only_b": fb.tell(), "mismatch": fm.tell()}
        save_checkpoint(pair_key, table, _ckpt_payload(prog, off))

    try:
        # Mỗi bên chuẩn hoá theo data_type CỦA CHÍNH NÓ (type có thể lệch giữa 2 DB).
        ia = _iter_rows(job.pool_a, table, key_cols, hash_cols, start_after, types_a)
        ib = _iter_rows(job.pool_b, table, key_cols, hash_cols, start_after, types_b)
        a = await anext(ia, None)
        b = await anext(ib, None)

        since_tick = 0
        tick_count = 0
        while a is not None and b is not None:
            ka, ha = a
            kb, hb = b
            if ka < kb:
                prog.only_in_a += 1
                _emit(fa, prog.sample_only_a, ka)
                _mark(ka)
                a = await anext(ia, None)
            elif ka > kb:
                prog.only_in_b += 1
                _emit(fb, prog.sample_only_b, kb)
                _mark(kb)
                b = await anext(ib, None)
            else:
                if ha != hb:
                    prog.value_mismatch += 1
                    _emit(fm, prog.sample_mismatch, ka)
                _mark(ka)
                a = await anext(ia, None)
                b = await anext(ib, None)
            prog.scanned += 1
            since_tick += 1
            if since_tick >= _PROGRESS_TICK:
                since_tick = 0
                tick_count += 1
                if checkpointable and tick_count % _CHECKPOINT_EVERY == 0:
                    _save_ckpt()
                await _notify(job)

        while a is not None:
            prog.only_in_a += 1
            _emit(fa, prog.sample_only_a, a[0])
            _mark(a[0])
            prog.scanned += 1
            a = await anext(ia, None)
        while b is not None:
            prog.only_in_b += 1
            _emit(fb, prog.sample_only_b, b[0])
            _mark(b[0])
            prog.scanned += 1
            b = await anext(ib, None)
    finally:
        for f in (fa, fb, fm):
            if f is not None:
                f.close()


def _fmt_val(v) -> str:
    if v is None:
        return "∅"
    s = str(v)
    return s if len(s) <= 120 else s[:117] + "…"


async def _fetch_mismatch_details(
    job: JobState, prog: TableProgress, table: str, hash_cols: list[str], pair_key: str
) -> None:
    """Stream TOÀN BỘ id mismatch từ spill, fetch giá trị 2 DB theo bó, ghi chi
    tiết cột khác ra spill 'mismatch_detail'. RAM chỉ giữ 1 bó + preview.

    MAX_MISMATCH_DETAIL > 0 → cap số id lấy chi tiết; = 0 → lấy full.
    Đặt prog.mismatch_row_count = tổng số DÒNG chi tiết (Σ cột khác / bản ghi) để
    export tính kích thước chia file.
    """
    spill.reset(pair_key, table, "mismatch_detail")
    col_list = ", ".join(_qident(c) for c in (["id"] + hash_cols))
    sql = f"SELECT {col_list} FROM {_qident(table)} WHERE id = ANY($1::bigint[])"
    cap = MAX_MISMATCH_DETAIL
    preview: list[dict] = []
    batch: list = []
    counters = {"rows": 0, "ids": 0}

    fd = spill.open_append(pair_key, table, "mismatch_detail")

    async def _flush() -> None:
        if not batch:
            return
        rows_a = await job.pool_a.fetch(sql, batch)
        rows_b = await job.pool_b.fetch(sql, batch)
        map_a = {r["id"]: r for r in rows_a}
        map_b = {r["id"]: r for r in rows_b}
        for rid in batch:
            ra, rb = map_a.get(rid), map_b.get(rid)
            diffs = []
            if ra is not None and rb is not None:
                for c in hash_cols:
                    if ra[c] != rb[c]:
                        diffs.append({"col": c, "a": _fmt_val(ra[c]), "b": _fmt_val(rb[c])})
            spill.write(fd, {"id": rid, "diffs": diffs})
            counters["rows"] += len(diffs) or 1  # ít nhất 1 dòng (id) kể cả khi rỗng
            if diffs and len(preview) < PREVIEW_SAMPLES:
                preview.append({"id": rid, "diffs": diffs})
        batch.clear()

    try:
        for rid in spill.iter_records(pair_key, table, "mismatch"):
            batch.append(rid)
            counters["ids"] += 1
            if len(batch) >= MISMATCH_DETAIL_BATCH:
                await _flush()
            if cap and counters["ids"] >= cap:
                break
        await _flush()
    finally:
        fd.close()

    prog.mismatch_details = preview
    prog.mismatch_row_count = counters["rows"]


# --------------------------------------------------------------------------- #
# Tìm cụm từ khóa trong dữ liệu (chỉ DB B, chỉ cột kiểu text)
# --------------------------------------------------------------------------- #
# Các data_type (information_schema) coi là "text" để dò ILIKE.
_TEXT_TYPES = {
    "character varying", "varchar", "text", "character", "char",
    "citext", "name", "json", "jsonb",
}


def _sql_literal(s: str) -> str:
    """Chuỗi literal SQL an toàn để NHÚNG vào câu query hiển thị (re-runnable)."""
    return "'" + s.replace("'", "''") + "'"


def _search_terms(job: JobState) -> list[tuple[str, str, str, str]]:
    """Danh sách (label, kind, operator, pattern) cần dò:
    - keyword tự nhập → ILIKE '%kw%'
    - bộ dò theo mẫu (config.SEARCH_PATTERNS) → ~* regex
    """
    from ..config import SEARCH_PATTERN_MAP

    terms: list[tuple[str, str, str, str]] = []
    for kw in job.keywords:
        terms.append((kw, "keyword", "ILIKE", f"%{kw}%"))
    for key in job.search_patterns:
        p = SEARCH_PATTERN_MAP.get(key)
        if p:
            terms.append((p["label"], "pattern", "~*", p["regex"]))
    return terms


async def _search_keywords(job: JobState, table: str) -> None:
    """Dò từng term (keyword + mẫu) trong các cột text của `table` ở DB B; ghi
    kết quả vào job.keyword_hits. Mỗi term khớp ≥1 dòng → 1 KeywordHit.

    Chạy độc lập với diff A↔B: chỉ liệt kê dữ liệu ở B khớp term cần tìm.
    Câu query lưu KHÔNG có LIMIT → copy chạy lại lấy đủ toàn bộ dòng khớp.
    Lỗi ở 1 bảng/term không làm hỏng cả compare — ghi lại thành 1 hit lỗi.
    """
    terms = _search_terms(job)
    if not terms:
        return
    pool = job.pool_b
    try:
        types = await _column_types(pool, table)
    except Exception as exc:  # noqa: BLE001
        job.keyword_hits.append(KeywordHit(
            keyword="(tất cả)", table=table, db_label=job.label_b,
            error=f"{type(exc).__name__}: {exc}",
        ))
        return

    text_cols = [c for c, t in types.items() if t in _TEXT_TYPES]
    if not text_cols:
        return  # bảng không có cột text → không thể chứa term
    has_id = "id" in types
    qtable = _qident(table)

    for (label, kind, op, pattern) in terms:
        predicate = " OR ".join(f"{_qident(c)}::text {op} $1" for c in text_cols)
        lit = _sql_literal(pattern)
        try:
            count = await pool.fetchval(
                f"SELECT count(*) FROM {qtable} WHERE {predicate}", pattern
            )
            if not count:
                continue
            sample_ids: list = []
            if has_id:
                # Câu query hiển thị/copy: KHÔNG LIMIT (lấy đủ). Peek id thì có
                # LIMIT riêng để cell không phình.
                display_query = (
                    f"SELECT id FROM {qtable} WHERE {predicate} ORDER BY id"
                ).replace("$1", lit)
                rows = await pool.fetch(
                    f"SELECT id FROM {qtable} WHERE {predicate} "
                    f"ORDER BY id LIMIT {KEYWORD_SAMPLE_IDS}",
                    pattern,
                )
                sample_ids = [r["id"] for r in rows]
            else:
                display_query = f"SELECT * FROM {qtable} WHERE {predicate}".replace("$1", lit)
            job.keyword_hits.append(KeywordHit(
                keyword=label, kind=kind, table=table, db_label=job.label_b,
                match_count=int(count), columns=list(text_cols),
                sample_ids=sample_ids, has_id=has_id, query=display_query,
            ))
        except Exception as exc:  # noqa: BLE001
            job.keyword_hits.append(KeywordHit(
                keyword=label, kind=kind, table=table, db_label=job.label_b,
                query=f"SELECT ... FROM {qtable} WHERE {predicate}".replace("$1", lit),
                error=f"{type(exc).__name__}: {exc}",
            ))


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
    job.keyword_hits = []  # quét lại từ đầu mỗi lần chạy compare
    job.keyword_running = False
    job.keyword_scanned_tables = 0

    do_compare = job.run_mode in ("both", "compare")
    do_keyword = job.run_mode in ("both", "keyword") and bool(job.keywords or job.search_patterns)
    job.keyword_total_tables = len(job.progress) if do_keyword else 0

    # Dọn spill rác của các bảng không còn được chọn (giữ bảng đang chọn → an
    # toàn cho resume). Spill của bảng đang chọn được _merge_join tự reset.
    spill.cleanup_pair(job.pair_key(), set(job.progress.keys()))
    sem = asyncio.Semaphore(MAX_WORKERS)

    async def _compare_guarded(table: str) -> None:
        async with sem:
            await _compare_one(job, table)

    async def _keyword_guarded(table: str) -> None:
        async with sem:
            await _search_keywords(job, table)
            job.keyword_scanned_tables += 1
            await _notify(job)

    try:
        # Phase compare A↔B (bỏ qua nếu chỉ tìm từ khóa).
        if do_compare:
            await asyncio.gather(*(_compare_guarded(t) for t in job.progress))
        else:
            # Keyword-only: đánh dấu bảng done ngay để bảng tiến độ hiển thị gọn,
            # không chạy diff.
            now = time.time()
            for prog in job.progress.values():
                prog.status = "done"
                prog.started_at = prog.finished_at = now
            await _notify(job)

        # Phase tìm cụm từ khóa (chạy sau khi compare xong → tách bạch, đồng hồ
        # phần này được phản ánh qua card "Bản ghi khớp từ khóa").
        if do_keyword:
            job.keyword_running = True
            await _notify(job)
            await asyncio.gather(*(_keyword_guarded(t) for t in job.progress))
    finally:
        job.keyword_running = False
        job.status = "done"
        job.compare_finished_at = time.time()
        await _notify(job)

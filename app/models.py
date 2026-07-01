"""Data model in-memory cho job state."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DSNConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.dbname}"
        )

    def safe(self) -> dict[str, Any]:
        """Dạng hiển thị/log — không lộ password."""
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
        }


@dataclass
class ColumnDiff:
    name: str
    kind: str  # added | dropped | type_changed | nullable_changed | default_changed
    detail_a: Optional[str] = None
    detail_b: Optional[str] = None


@dataclass
class ConstraintDiff:
    name: str
    # Loại constraint: p=primary key, u=unique, f=foreign key, c=check.
    contype: str
    kind: str  # added | dropped | changed
    def_a: Optional[str] = None
    def_b: Optional[str] = None

    _LABELS = {"p": "primary key", "u": "unique", "f": "foreign key", "c": "check"}

    @property
    def type_label(self) -> str:
        return self._LABELS.get(self.contype, self.contype)


@dataclass
class TableSchemaDiff:
    name: str
    status: str  # identical | changed | new | dropped
    is_custom: bool
    columns: list[ColumnDiff] = field(default_factory=list)
    constraints: list[ConstraintDiff] = field(default_factory=list)

    @property
    def added(self) -> list[ColumnDiff]:
        return [c for c in self.columns if c.kind == "added"]

    @property
    def dropped(self) -> list[ColumnDiff]:
        return [c for c in self.columns if c.kind == "dropped"]

    @property
    def type_changed(self) -> list[ColumnDiff]:
        return [c for c in self.columns if c.kind == "type_changed"]

    @property
    def nullable_changed(self) -> list[ColumnDiff]:
        return [c for c in self.columns if c.kind == "nullable_changed"]

    # --- constraint diff helpers ---
    @property
    def con_added(self) -> list[ConstraintDiff]:
        return [c for c in self.constraints if c.kind == "added"]

    @property
    def con_dropped(self) -> list[ConstraintDiff]:
        return [c for c in self.constraints if c.kind == "dropped"]

    @property
    def con_changed(self) -> list[ConstraintDiff]:
        return [c for c in self.constraints if c.kind == "changed"]


@dataclass
class KeywordHit:
    """1 dòng kết quả tìm từ khóa: 1 keyword khớp trong 1 bảng (ở DB B).

    query giữ nguyên câu SQL re-runnable (đã nhúng literal pattern) để dán chạy
    lại thủ công. error khác rỗng → dòng này là lỗi khi quét bảng đó.
    """
    keyword: str
    table: str
    db_label: str
    kind: str = "keyword"  # keyword (tự nhập) | pattern (email/link/domain…)
    match_count: int = 0
    columns: list[str] = field(default_factory=list)  # các cột text đã DÒ
    # Các cột THỰC SỰ chứa dữ liệu match, kèm câu UPDATE replace:
    # [{"col": str, "count": int, "type": str, "replace": "UPDATE ... @REPL@ ..."}]
    matched: list[dict] = field(default_factory=list)
    sample_ids: list[Any] = field(default_factory=list)
    has_id: bool = False
    query: str = ""  # câu SELECT xem toàn bộ bản ghi match (không LIMIT)
    error: str = ""


@dataclass
class TableEstimate:
    name: str
    rows_a: int = 0
    rows_b: int = 0
    has_id: bool = False
    suggested_mode: str = "count-only"


@dataclass
class TableProgress:
    name: str
    mode: str
    status: str = "queued"  # queued | running | done | warning | error
    scanned: int = 0
    total: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Kết quả compare
    count_a: int = 0
    count_b: int = 0
    only_in_a: int = 0
    only_in_b: int = 0
    value_mismatch: int = 0
    sample_only_a: list[Any] = field(default_factory=list)
    sample_only_b: list[Any] = field(default_factory=list)
    sample_mismatch: list[Any] = field(default_factory=list)
    # Chi tiết bản ghi cùng id nhưng khác giá trị (PREVIEW ~PREVIEW_SAMPLES bản
    # cho HTML; full nằm ở spill file mismatch_detail):
    # [{"id": x, "diffs": [{"col": c, "a": "...", "b": "..."}]}]
    mismatch_details: list[dict] = field(default_factory=list)
    # Tổng số DÒNG chi tiết mismatch đã ghi ra spill (= Σ số cột khác / bản ghi).
    # Export dùng để tính kích thước chia file mà không cần đọc lại spill.
    mismatch_row_count: int = 0
    column_scope: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""
    # Checkpoint resume: id lớn nhất đã so khớp xong cả 2 bên. Lần chạy sau
    # tiếp tục từ id > resume_after_id (compare luôn sort id tăng dần).
    resume_after_id: Optional[int] = None
    # True nếu lần chạy này tiếp tục từ checkpoint cũ thay vì quét lại từ đầu.
    resumed: bool = False

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def count_delta(self) -> int:
        return self.count_b - self.count_a

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 100 if self.status in ("done", "warning") else 0
        return min(100, int(self.scanned * 100 / self.total))

    @property
    def sort_rank(self) -> int:
        """Chưa xong (running/queued) lên trên, đã xong xuống dưới."""
        return {"running": 0, "queued": 1}.get(self.status, 2)


@dataclass
class JobState:
    id: str
    created_at: float = field(default_factory=time.time)
    status: str = "config"  # config | schema | selection | running | done

    label_a: str = "Odoo 17"
    label_b: str = "Odoo 19"
    dsn_a: Optional[DSNConfig] = None
    dsn_b: Optional[DSNConfig] = None
    pool_a: Any = None  # asyncpg.Pool
    pool_b: Any = None
    connected_a: bool = False
    connected_b: bool = False
    version_a: str = ""
    version_b: str = ""
    error_a: str = ""
    error_b: str = ""

    prefixes: list[str] = field(default_factory=list)
    # Cụm từ khóa cần tìm trong dữ liệu (nhập ở bước Config, ngăn cách bằng ';').
    keywords: list[str] = field(default_factory=list)
    # Các bộ dò theo mẫu được bật (key trong config.SEARCH_PATTERNS): email/link/…
    search_patterns: list[str] = field(default_factory=list)
    # Kết quả tìm keyword (quét ở DB B trong lúc compare) — 1 phần tử / (keyword, bảng).
    keyword_hits: list[KeywordHit] = field(default_factory=list)
    # Chế độ chạy bước 4: both = compare + tìm từ khóa · compare · keyword.
    run_mode: str = "both"
    keyword_running: bool = False
    keyword_scanned_tables: int = 0
    keyword_total_tables: int = 0

    # Phase 1
    schema_tables: dict[str, TableSchemaDiff] = field(default_factory=dict)
    # Phase 2
    estimates: dict[str, TableEstimate] = field(default_factory=dict)
    selection: dict[str, str] = field(default_factory=dict)  # table -> mode
    # Phase 3
    progress: dict[str, TableProgress] = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    compare_task: Any = None
    compare_started_at: Optional[float] = None
    compare_finished_at: Optional[float] = None

    @property
    def both_connected(self) -> bool:
        return self.connected_a and self.connected_b

    def pair_key(self) -> str:
        """Khoá định danh cặp DB để gom checkpoint (độc lập với job_id)."""
        def part(d: Optional[DSNConfig]) -> str:
            return f"{d.host}:{d.port}/{d.dbname}" if d else "?"

        return f"{part(self.dsn_a)}__{part(self.dsn_b)}"

    def is_custom(self, table: str) -> bool:
        return any(table.startswith(p) for p in self.prefixes)

    # --- keyword search counters ---
    @property
    def keyword_match_total(self) -> int:
        """Tổng số DÒNG (bản ghi) khớp từ khóa, cộng dồn mọi (keyword, bảng)."""
        return sum(h.match_count for h in self.keyword_hits if not h.error)

    @property
    def keyword_hit_tables(self) -> int:
        """Số bảng có ≥1 dòng khớp từ khóa."""
        return len({h.table for h in self.keyword_hits if not h.error and h.match_count})

    # --- counters cho màn progress / report ---
    def counters(self) -> dict[str, int]:
        done = warning = error = queued = running = 0
        for p in self.progress.values():
            if p.status == "done":
                done += 1
            elif p.status == "warning":
                warning += 1
            elif p.status == "error":
                error += 1
            elif p.status == "running":
                running += 1
            else:
                queued += 1
        return {
            "done": done,
            "warning": warning,
            "error": error,
            "queued": queued,
            "running": running,
            "total": len(self.progress),
        }

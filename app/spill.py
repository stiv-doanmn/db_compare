"""Spill files — dùng file JSONL trên đĩa như "database tạm" cho dữ liệu sai
lệch khối lượng lớn, tránh giữ hàng triệu bản ghi trong RAM.

Mỗi (pair_key, table, kind) → 1 file ``data/spill/<pair>/<table>.<kind>.jsonl``,
mỗi dòng là 1 JSON record. kind:
- only_a / only_b : key (id hoặc tuple) của bản ghi chỉ có ở 1 bên.
- mismatch        : key của bản ghi cùng id nhưng khác giá trị (chưa có chi tiết).
- mismatch_detail : {"id": .., "diffs": [{"col","a","b"}, ...]} — chi tiết cột khác.

Ghi ở chế độ NHỊ PHÂN (utf-8 encode tay) để ``tell()`` trả về offset byte thật,
phục vụ truncate-resume khớp checkpoint (xem data_compare).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, BinaryIO, Iterator

_ROOT = Path(__file__).resolve().parent.parent
_SPILL_ROOT = _ROOT / "data" / "spill"

KINDS = ("only_a", "only_b", "mismatch", "mismatch_detail")


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in name) or "_"


def _dir(pair_key: str) -> Path:
    return _SPILL_ROOT / _safe(pair_key)


def path(pair_key: str, table: str, kind: str) -> Path:
    return _dir(pair_key) / f"{_safe(table)}.{kind}.jsonl"


def reset(pair_key: str, table: str, *kinds: str) -> None:
    """Xoá file spill (toàn bộ kind nếu không chỉ định) — gọi khi chạy lại từ đầu."""
    for k in (kinds or KINDS):
        try:
            path(pair_key, table, k).unlink()
        except FileNotFoundError:
            pass


def open_append(pair_key: str, table: str, kind: str) -> BinaryIO:
    p = path(pair_key, table, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.open("ab")


def write(fh: BinaryIO, obj: Any) -> None:
    fh.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


def truncate(pair_key: str, table: str, kind: str, size: int) -> None:
    """Cắt file về đúng `size` byte (bỏ phần ghi sau checkpoint cuối khi resume)."""
    p = path(pair_key, table, kind)
    try:
        with p.open("r+b") as fh:
            fh.truncate(size)
    except FileNotFoundError:
        pass


def cleanup_pair(pair_key: str, keep_tables: set[str]) -> None:
    """Xoá spill của các bảng KHÔNG còn trong keep_tables (dọn rác lần chọn trước).

    An toàn cho resume: bảng đang chọn (kể cả đang resume) đều nằm trong
    keep_tables nên được giữ.
    """
    d = _dir(pair_key)
    if not d.exists():
        return
    keep = {_safe(t) for t in keep_tables}
    for f in d.iterdir():
        if not f.is_file():
            continue
        for k in KINDS:
            suffix = f".{k}.jsonl"
            if f.name.endswith(suffix):
                if f.name[: -len(suffix)] not in keep:
                    try:
                        f.unlink()
                    except FileNotFoundError:
                        pass
                break


def clear_pair(pair_key: str) -> None:
    """Xoá toàn bộ spill của 1 cặp DB (giải phóng đĩa; report sẽ không còn full)."""
    shutil.rmtree(_dir(pair_key), ignore_errors=True)


def iter_records(pair_key: str, table: str, kind: str) -> Iterator[Any]:
    """Đọc lần lượt từng record; bỏ qua dòng hỏng (do crash giữa chừng)."""
    p = path(pair_key, table, kind)
    try:
        fh = p.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

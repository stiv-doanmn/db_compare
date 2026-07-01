"""JSON file dùng như database nhẹ — persist credential (không pass) + checkpoint compare.

Cấu trúc file ``data/store.json``::

    {
      "connections": {
        "a": {"label","host","port","dbname","user"},   # KHÔNG lưu password
        "b": {...},
        "prefixes": ["nev_", ...]
      },
      "checkpoints": {
        "<pair_key>": {
          "<table>": {
            "mode", "status", "resume_after_id", "scanned",
            "count_a","count_b","only_in_a","only_in_b","value_mismatch",
            "error", "updated_at"
          }
        }
      }
    }

Checkpoint cho phép: nếu 1 bảng bị lỗi giữa chừng ở bước 4, lưu lại id cuối cùng
đã so khớp (``resume_after_id``). Lần chạy sau so tiếp từ ``id > resume_after_id``
(compare luôn ORDER BY id tăng dần) thay vì quét lại từ đầu.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"
_STORE_PATH = _DATA_DIR / "store.json"

_lock = threading.Lock()


def _empty() -> dict[str, Any]:
    return {"connections": {}, "checkpoints": {}}


def _read() -> dict[str, Any]:
    try:
        with _STORE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("connections", {})
        data.setdefault("checkpoints", {})
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty()


def _write(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    tmp.replace(_STORE_PATH)  # atomic trên cùng filesystem


# --------------------------------------------------------------------------- #
# Connections (credential — không password)
# --------------------------------------------------------------------------- #
def save_connections(
    *,
    label_a: str,
    label_b: str,
    dsn_a: Any,
    dsn_b: Any,
    prefixes: list[str],
    keywords: list[str] | None = None,
) -> None:
    """Lưu thông tin kết nối gần nhất để lần sau prefill form (bỏ password)."""
    with _lock:
        data = _read()
        data["connections"] = {
            "a": {
                "label": label_a,
                "host": dsn_a.host,
                "port": dsn_a.port,
                "dbname": dsn_a.dbname,
                "user": dsn_a.user,
            },
            "b": {
                "label": label_b,
                "host": dsn_b.host,
                "port": dsn_b.port,
                "dbname": dsn_b.dbname,
                "user": dsn_b.user,
            },
            "prefixes": list(prefixes),
            "keywords": list(keywords or []),
            "updated_at": time.time(),
        }
        _write(data)


def load_connections() -> Optional[dict[str, Any]]:
    with _lock:
        conns = _read().get("connections") or {}
    return conns if conns.get("a") and conns.get("b") else None


# --------------------------------------------------------------------------- #
# Checkpoints (resume compare theo từng bảng)
# --------------------------------------------------------------------------- #
def save_checkpoint(pair_key: str, table: str, payload: dict[str, Any]) -> None:
    with _lock:
        data = _read()
        cps = data.setdefault("checkpoints", {}).setdefault(pair_key, {})
        payload = dict(payload)
        payload["updated_at"] = time.time()
        cps[table] = payload
        _write(data)


def load_checkpoint(pair_key: str, table: str) -> Optional[dict[str, Any]]:
    with _lock:
        return (
            _read()
            .get("checkpoints", {})
            .get(pair_key, {})
            .get(table)
        )


def load_checkpoints(pair_key: str) -> dict[str, Any]:
    with _lock:
        return dict(_read().get("checkpoints", {}).get(pair_key, {}))


def clear_checkpoint(pair_key: str, table: str) -> None:
    with _lock:
        data = _read()
        cps = data.get("checkpoints", {}).get(pair_key)
        if cps and table in cps:
            cps.pop(table, None)
            _write(data)

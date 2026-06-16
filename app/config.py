"""Cấu hình toàn cục đọc từ environment."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Số bảng được compare song song — semaphore tránh overload DB.
MAX_WORKERS: int = _int("MAX_WORKERS", 4)

# Kích thước batch khi keyset pagination quét row hash.
BATCH_SIZE: int = _int("BATCH_SIZE", 1000)

# Ngưỡng cảnh báo bảng lớn → auto-suggest count-only.
LARGE_TABLE_THRESHOLD: int = _int("LARGE_TABLE_THRESHOLD", 1_000_000)

# Số lượng id mẫu giữ lại cho mỗi loại khác biệt (missing / mismatch).
MAX_SAMPLES: int = _int("MAX_SAMPLES", 20)

# asyncpg pool size cho mỗi DB.
POOL_MIN_SIZE: int = _int("POOL_MIN_SIZE", 1)
POOL_MAX_SIZE: int = _int("POOL_MAX_SIZE", 5)

# Prefix mặc định nhận diện bảng custom.
DEFAULT_PREFIXES: list[str] = [
    p.strip() for p in os.getenv("DEFAULT_PREFIXES", "nev_,astra_,helpdesk,audit,sh").split(",") if p.strip()
]

# Nhãn mặc định cho 2 DB.
DEFAULT_LABEL_A: str = os.getenv("DEFAULT_LABEL_A", "Odoo 17")
DEFAULT_LABEL_B: str = os.getenv("DEFAULT_LABEL_B", "Odoo 19")

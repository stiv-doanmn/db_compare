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

# Số bản ghi sai lệch tối đa cho MỖI FILE .xlsx khi export (kích thước chia file).
# Engine GIỮ TOÀN BỘ bản ghi sai lệch trong RAM (không cắt mẫu) rồi export chia
# thành nhiều file theo ngưỡng này — vd 800k diff → file1 500k + file2 300k.
# Phải < 1.048.576 (trần dòng/sheet của Excel).
MAX_SAMPLES: int = _int("MAX_SAMPLES", 500_000)

# Giới hạn số id mismatch được fetch chi tiết cột. 0 = KHÔNG giới hạn (lấy full,
# ghi ra spill file để khỏi chết RAM). Đặt >0 nếu muốn cap lại cho nhẹ.
MAX_MISMATCH_DETAIL: int = _int("MAX_MISMATCH_DETAIL", 0)

# Số id mỗi lần query khi fetch chi tiết mismatch (bó nhỏ → giới hạn RAM/round-trip).
MISMATCH_DETAIL_BATCH: int = _int("MISMATCH_DETAIL_BATCH", 1_000)

# Số bản ghi giữ trong RAM làm PREVIEW cho HTML/progress (full nằm ở spill file).
PREVIEW_SAMPLES: int = _int("PREVIEW_SAMPLES", 100)

# Số id mẫu tối đa lấy cho mỗi keyword-hit (báo cáo tìm từ khóa). Chỉ để tham
# khảo; số dòng khớp thật (match_count) luôn đếm đủ bằng COUNT(*).
KEYWORD_SAMPLE_IDS: int = _int("KEYWORD_SAMPLE_IDS", 50)

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

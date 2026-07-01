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

# Số id mẫu tối đa NẠP để hiển thị peek trong cell (chỉ để xem nhanh). Câu query
# xuất ra KHÔNG bị LIMIT — copy chạy lại lấy đủ toàn bộ dòng khớp.
KEYWORD_SAMPLE_IDS: int = _int("KEYWORD_SAMPLE_IDS", 50)

# Bộ dò theo mẫu (regex Postgres ARE, so bằng ~* — không phân biệt hoa/thường).
# Người dùng bật/tắt ở bước Config, chạy song song với keyword tự nhập.
SEARCH_PATTERNS: list[dict] = [
    {"key": "email", "label": "Email", "desc": "Địa chỉ email",
     "regex": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"},
    {"key": "link", "label": "Link (URL)", "desc": "http:// hoặc https://",
     "regex": r"https?://[^[:space:]]+"},
    {"key": "domain", "label": "Domain", "desc": "Tên miền có TLD thật (com/net/vn/io…), loại đuôi file .png/.pdf",
     "regex": r"[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9-]+)*\.(com|net|org|edu|gov|mil|int|io|co|dev|app|vn|info|biz|xyz|me|cloud|ai|tech|online|store|site|shop)(?![A-Za-z])"},
    {"key": "ip", "label": "IP (IPv4)", "desc": "Địa chỉ IPv4",
     "regex": r"([0-9]{1,3}\.){3}[0-9]{1,3}"},
    {"key": "phone", "label": "Số điện thoại", "desc": "Chuỗi ≥8 chữ số (có thể có +, -, khoảng trắng)",
     "regex": r"(\+?[0-9][0-9 .-]{7,}[0-9])"},
]
SEARCH_PATTERN_MAP: dict[str, dict] = {p["key"]: p for p in SEARCH_PATTERNS}

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

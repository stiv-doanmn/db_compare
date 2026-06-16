# Odoo DB Compare

Công cụ so sánh schema + dữ liệu giữa 2 database Odoo (vd Odoo 17 ↔ Odoo 19).
SSR bằng FastAPI + Jinja2 + HTMX, realtime qua SSE. Không cần build step.

## Luồng 5 phase

1. **Config** — nhập DSN 2 DB trên UI, test connection.
2. **Schema diff** — query `information_schema`, so sánh columns/types/nullable/default,
   phân loại custom (theo prefix) / base, filter All/Changed/New/Dropped/Identical.
3. **Chọn bảng** — bảng có ở cả 2 DB kèm row estimate (`pg_class.reltuples`),
   auto-suggest mode (full / intersection / count-only), user override.
4. **Compare** — chạy song song (semaphore `MAX_WORKERS`), progress realtime qua SSE.
5. **Report** — summary cards + accordion từng bảng, export JSON / HTML.

## Logic data compare

| Mode         | Làm gì                                                                    |
|--------------|---------------------------------------------------------------------------|
| count-only   | `COUNT(*)` 2 bên → Δ rows                                                  |
| intersection | merge-join theo `id` trên MD5 row-hash, chỉ các cột chung 2 DB             |
| full         | như intersection nhưng hash toàn bộ cột chung; cảnh báo nếu schema lệch    |

Merge-join quét 1 lượt theo `id` (keyset pagination, batch `BATCH_SIZE`) phát hiện
đồng thời: id chỉ có ở A, id chỉ có ở B, id chung nhưng giá trị khác. Bảng không có
cột `id` → tự fallback `count-only`.

## Chạy local

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
uvicorn app.main:app --reload
# http://localhost:8000
```

## Chạy bằng Docker

```bash
docker compose up --build
# http://localhost:8000
```

Khi Postgres chạy trên host (Windows/Mac), trong form DSN dùng host
`host.docker.internal`.

## Cấu hình (env)

| Biến                  | Mặc định      | Ý nghĩa                                  |
|-----------------------|---------------|------------------------------------------|
| `MAX_WORKERS`         | 4             | Số bảng compare song song                |
| `BATCH_SIZE`          | 1000          | Batch keyset pagination                  |
| `LARGE_TABLE_THRESHOLD` | 1000000     | Ngưỡng auto-suggest count-only           |
| `DEFAULT_PREFIXES`    | `nev_,astra_` | Prefix nhận diện bảng custom             |

## Lưu ý

- Job state giữ **in-memory** theo `job_id` (UUID) — restart là mất.
- Không có auth (internal tool). Nên dùng **read-only** Postgres user.
- `reltuples` là estimate (cập nhật khi `ANALYZE`), không chính xác 100%.
- Rename column không tự detect → báo là dropped + added.

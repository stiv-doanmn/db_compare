# Odoo DB Compare

Công cụ so sánh schema + dữ liệu giữa 2 database Odoo (vd Odoo 17 ↔ Odoo 19).
SSR bằng FastAPI + Jinja2 + HTMX, realtime qua SSE. Không cần build step.

## Luồng 5 phase

1. **Config** — nhập DSN 2 DB trên UI, test connection.
2. **Schema diff** — query `information_schema`, so sánh columns/types/nullable/default,
   phân loại custom (theo prefix) / base, filter All/Changed/New/Dropped/Identical.
   Kèm so **constraint** (`pg_get_constraintdef`): foreign key, check, unique, primary key
   — phát hiện chỉ-có-ở-1-bên / đổi định nghĩa. Bảng cột giống nhau nhưng constraint lệch
   được nâng status `identical → changed`. (Index thường không xét.)
3. **Chọn bảng** — bảng có ở cả 2 DB kèm row estimate (`pg_class.reltuples`),
   auto-suggest mode (full / count-only), user override.
4. **Compare** — chạy song song (semaphore `MAX_WORKERS`), progress realtime qua SSE
   (server gửi JSON snapshot coalesce ~4fps, client patch DOM từng ô — không re-render
   cả bảng nên không giật). Lỗi giữa chừng được checkpoint để chạy lại resume từ id dở.
5. **Report** — summary cards + bảng tổng hợp (search theo tên) + accordion từng bảng,
   export **JSON / HTML / Excel (.xlsx) / CSV**. Excel: 1 sheet tổng quan + mỗi bảng 1
   sheet, tô màu theo trạng thái, chỉ rõ khác ở đâu (chỉ-ở-A / chỉ-ở-B / cùng id khác giá trị).

## Logic data compare

| Mode       | Làm gì                                                                      |
|------------|-----------------------------------------------------------------------------|
| count-only | `COUNT(*)` 2 bên → Δ rows                                                    |
| full       | merge-join theo `id` trên MD5 row-hash của **tất cả cột chung** 2 DB         |

Merge-join quét 1 lượt theo `id` (keyset pagination, batch `BATCH_SIZE`) phát hiện
đồng thời: id chỉ có ở A, id chỉ có ở B, id chung nhưng giá trị khác — **toàn bộ
row, toàn bộ cột chung**, không lấy mẫu (chỉ giữ tối đa `MAX_SAMPLES` id mẫu để
hiển thị). Cột chỉ có ở 1 bên không so giá trị được → báo ở schema diff. Bảng
không có cột `id` → composite key theo toàn bộ cột chung.

**Giảm false positive khi 2 DB khác phiên bản PostgreSQL**: pool ép `DateStyle`,
`TimeZone`, `IntervalStyle`, `extra_float_digits` giống nhau; row-hash chuẩn hoá
`numeric` (bỏ trailing-zero) và `json → jsonb` trước khi MD5.

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

## Store JSON (`data/store.json`)

DB nhẹ dạng JSON, persist qua restart (mount volume trong Docker):

- **Credential** 2 DB (host/port/dbname/user + label + prefixes) — **không lưu password**.
  Lần mở app sau tự prefill form Config.
- **Checkpoint** compare theo từng bảng + cặp DB: nếu bảng đang scan ở bước 4 bị lỗi,
  lưu lại `resume_after_id` (id lớn nhất đã so khớp xong). Chạy lại sẽ tiếp tục từ
  `id > resume_after_id` (compare luôn `ORDER BY id` tăng dần) thay vì quét lại từ đầu.
  Quét xong bảng → checkpoint tự xoá. (Chỉ áp dụng bảng có cột `id`.)

## Lưu ý

- Job state (pool, kết quả compare) giữ **in-memory** theo `job_id` (UUID) — restart là mất;
  mở lại URL job cũ → **tự tạo job mới** về bước Config (credential vẫn prefill từ store).
- Không có auth (internal tool). Nên dùng **read-only** Postgres user.
- `reltuples` là estimate (cập nhật khi `ANALYZE`), không chính xác 100%.
- Rename column không tự detect → báo là dropped + added.

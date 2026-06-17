"""FastAPI app — Odoo DB Compare Tool."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import store
from .config import DEFAULT_LABEL_A, DEFAULT_LABEL_B, DEFAULT_PREFIXES
from .db.constraint_diff import run_constraint_diff
from .db.data_compare import run_compare
from .db.estimator import build_estimates, estimate_time
from .db.pool import close_pool, create_pool, test_connection
from .db.schema_diff import run_schema_diff
from .export import build_csv, build_workbook
from .jobs.manager import manager
from .models import DSNConfig, TableProgress

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["estimate_time"] = estimate_time
templates.env.globals["now"] = time.time

app = FastAPI(title="Odoo DB Compare")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await manager.close_all()


def _not_found(job_id: str) -> RedirectResponse:
    """Job không tồn tại (vd server restart → mất in-memory state) → auto tạo
    job mới và đưa về bước Config thay vì báo 404."""
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- #
# Phase 1 — Config
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    """Tạo job mới rồi redirect tới URL có job_id (PRG — back-button an toàn)."""
    job = manager.new()
    job.prefixes = list(DEFAULT_PREFIXES)
    job.label_a = DEFAULT_LABEL_A
    job.label_b = DEFAULT_LABEL_B

    # Prefill từ kết nối đã lưu lần trước (store JSON, không có password).
    saved = store.load_connections()
    if saved:
        ca, cb = saved["a"], saved["b"]
        job.label_a = ca.get("label") or job.label_a
        job.label_b = cb.get("label") or job.label_b
        job.prefixes = saved.get("prefixes") or job.prefixes
        job.dsn_a = DSNConfig(ca["host"], ca["port"], ca["dbname"], ca["user"], "")
        job.dsn_b = DSNConfig(cb["host"], cb["port"], cb["dbname"], cb["user"], "")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def config_page(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    return templates.TemplateResponse(
        "config.html", {"request": request, "job": job}
    )


@app.post("/jobs/{job_id}/connect", response_class=HTMLResponse)
async def connect(
    request: Request,
    job_id: str,
    label_a: str = Form(DEFAULT_LABEL_A),
    label_b: str = Form(DEFAULT_LABEL_B),
    a_host: str = Form(...),
    a_port: int = Form(5432),
    a_dbname: str = Form(...),
    a_user: str = Form(...),
    a_password: str = Form(""),
    b_host: str = Form(...),
    b_port: int = Form(5432),
    b_dbname: str = Form(...),
    b_user: str = Form(...),
    b_password: str = Form(""),
    prefixes: str = Form(""),
):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)

    job.label_a, job.label_b = label_a.strip() or "DB A", label_b.strip() or "DB B"
    job.prefixes = [p.strip() for p in prefixes.split(",") if p.strip()]
    job.dsn_a = DSNConfig(a_host, a_port, a_dbname, a_user, a_password)
    job.dsn_b = DSNConfig(b_host, b_port, b_dbname, b_user, b_password)

    # Lưu credential (KHÔNG password) để lần sau prefill form.
    store.save_connections(
        label_a=job.label_a, label_b=job.label_b,
        dsn_a=job.dsn_a, dsn_b=job.dsn_b, prefixes=job.prefixes,
    )

    # Đóng pool cũ nếu test lại
    await close_pool(job.pool_a)
    await close_pool(job.pool_b)
    job.pool_a = job.pool_b = None
    job.connected_a = job.connected_b = False
    job.error_a = job.error_b = ""

    for side in ("a", "b"):
        dsn = job.dsn_a if side == "a" else job.dsn_b
        try:
            pool = await create_pool(dsn)
            version = await test_connection(pool)
            if side == "a":
                job.pool_a, job.connected_a, job.version_a = pool, True, version
            else:
                job.pool_b, job.connected_b, job.version_b = pool, True, version
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if side == "a":
                job.error_a = msg
            else:
                job.error_b = msg

    return templates.TemplateResponse(
        "partials/connection_status.html", {"request": request, "job": job}
    )


# --------------------------------------------------------------------------- #
# Phase 1 result — Schema diff
# --------------------------------------------------------------------------- #
@app.post("/jobs/{job_id}/schema-diff")
async def run_schema_diff_route(job_id: str):
    """Chạy diff rồi redirect (PRG) → back về xem lại được state đã cache."""
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    if not job.both_connected:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    await run_schema_diff(job)
    await run_constraint_diff(job)
    await build_estimates(job)
    job.status = "schema"
    return RedirectResponse(f"/jobs/{job_id}/schema-diff", status_code=303)


@app.get("/jobs/{job_id}/schema-diff", response_class=HTMLResponse)
async def schema_diff_page(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    if not job.schema_tables:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    return templates.TemplateResponse(
        "schema_diff.html", {"request": request, "job": job}
    )


# --------------------------------------------------------------------------- #
# Phase 2 — Table selection
# --------------------------------------------------------------------------- #
@app.get("/jobs/{job_id}/selection", response_class=HTMLResponse)
async def selection(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    if not job.estimates:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    job.status = "selection"
    return templates.TemplateResponse(
        "selection.html", {"request": request, "job": job}
    )


@app.post("/jobs/{job_id}/compare")
async def compare(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)

    form = await request.form()
    selected = form.getlist("select")
    job.selection = {}
    job.progress = {}
    job.queue = asyncio.Queue()

    for table in selected:
        if table not in job.estimates:
            continue
        mode = form.get(f"mode_{table}", "count-only")
        job.selection[table] = mode
        job.progress[table] = TableProgress(name=table, mode=mode)

    if not job.progress:
        return RedirectResponse(f"/jobs/{job_id}/selection", status_code=303)

    job.compare_task = asyncio.create_task(run_compare(job))
    return RedirectResponse(f"/jobs/{job_id}/progress", status_code=303)


@app.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
async def progress_page(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    if not job.progress:
        return RedirectResponse(f"/jobs/{job_id}/selection", status_code=303)
    return templates.TemplateResponse(
        "progress.html", {"request": request, "job": job}
    )


# --------------------------------------------------------------------------- #
# Phase 3 — SSE progress stream (JSON, coalesced — patch DOM kiểu React)
# --------------------------------------------------------------------------- #
def _progress_snapshot(job) -> dict:
    """Snapshot gọn cho client patch DOM — không render HTML phía server."""
    running = job.compare_finished_at is None and job.status != "done"
    end = job.compare_finished_at or time.time()
    elapsed = end - job.compare_started_at if job.compare_started_at else 0.0
    return {
        "status": job.status,
        "running": running,
        "elapsed": round(elapsed, 1),
        "counters": job.counters(),
        "report_url": f"/jobs/{job.id}/report",
        "tables": [
            {
                "name": p.name,
                "mode": p.mode,
                "status": p.status,
                "percent": 100 if p.status in ("done", "warning", "error") else p.percent,
                "elapsed": round(p.elapsed, 1),
                "count_a": p.count_a,
                "count_b": p.count_b,
                "delta": p.count_delta,
                "only_in_a": p.only_in_a,
                "only_in_b": p.only_in_b,
                "value_mismatch": p.value_mismatch,
                "resumed": p.resumed,
                "note": p.note,
                "error": p.error,
            }
            for p in job.progress.values()
        ],
    }


# Tần suất tối đa đẩy snapshot xuống FE (giây) — coalesce burst notify → hết giật.
_EMIT_INTERVAL = 0.25


@app.get("/jobs/{job_id}/state")
async def state_stream(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)

    async def gen():
        # Phát ngay snapshot đầu để FE dựng bảng.
        yield {"event": "state", "data": json.dumps(_progress_snapshot(job))}
        while True:
            if await request.is_disconnected():
                break
            # Chờ ít nhất 1 tick (hoặc heartbeat 1s), rồi gộp mọi tick tồn đọng.
            try:
                await asyncio.wait_for(job.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            else:
                while not job.queue.empty():  # drain burst → 1 lần render
                    job.queue.get_nowait()

            yield {"event": "state", "data": json.dumps(_progress_snapshot(job))}
            if job.status == "done":
                break
            # Giới hạn nhịp phát: tránh flood FE khi tick dồn dập.
            await asyncio.sleep(_EMIT_INTERVAL)

    return EventSourceResponse(gen())


# --------------------------------------------------------------------------- #
# Phase 4 — Report
# --------------------------------------------------------------------------- #
@app.get("/jobs/{job_id}/report", response_class=HTMLResponse)
async def report(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    return templates.TemplateResponse(
        "report.html", {"request": request, "job": job, "standalone": False}
    )


def _report_dict(job) -> dict:
    return {
        "job_id": job.id,
        "label_a": job.label_a,
        "label_b": job.label_b,
        "version_a": job.version_a,
        "version_b": job.version_b,
        "counters": job.counters(),
        "tables": [
            {
                "name": p.name,
                "mode": p.mode,
                "status": p.status,
                "count_a": p.count_a,
                "count_b": p.count_b,
                "count_delta": p.count_delta,
                "only_in_a": p.only_in_a,
                "only_in_b": p.only_in_b,
                "value_mismatch": p.value_mismatch,
                "sample_only_a": p.sample_only_a,
                "sample_only_b": p.sample_only_b,
                "sample_mismatch": p.sample_mismatch,
                "mismatch_details": p.mismatch_details,
                "column_scope": p.column_scope,
                "elapsed": round(p.elapsed, 2),
                "note": p.note,
                "error": p.error,
            }
            for p in job.progress.values()
        ],
    }


@app.get("/jobs/{job_id}/report.json")
async def report_json(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_report_dict(job))


@app.get("/jobs/{job_id}/report.html")
async def report_html_download(request: Request, job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    html = templates.get_template("report.html").render(
        request=request, job=job, standalone=True
    )
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f'attachment; filename="compare_{job_id[:8]}.html"'
        },
    )


@app.get("/jobs/{job_id}/report.xlsx")
async def report_xlsx(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    content = build_workbook(job)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="compare_{job_id[:8]}.xlsx"'
        },
    )


@app.get("/jobs/{job_id}/report.csv")
async def report_csv(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return _not_found(job_id)
    content = build_csv(job)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="compare_{job_id[:8]}.csv"'
        },
    )

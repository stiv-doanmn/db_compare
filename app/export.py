"""Phase 5 export — Excel (.xlsx) và CSV báo cáo so sánh.

Excel: 1 sheet tổng quan + mỗi bảng 1 sheet riêng, tô màu theo trạng thái,
chỉ rõ khác ở đâu (chỉ có ở A / chỉ có ở B / cùng id khác giá trị → cột nào).
CSV: 1 file phẳng, mỗi dòng là 1 phát hiện (finding) — dễ import/pivot.
"""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.worksheet import Worksheet

# ----- Bảng màu chuẩn doanh nghiệp ----------------------------------------- #
_NAVY = "181D26"
_WHITE = "FFFFFF"
_GREY_SOFT = "F1F3F5"
_GREY_LINE = "DDDDDD"

_FILL_HEAD = PatternFill("solid", fgColor=_NAVY)
_FILL_SUBHEAD = PatternFill("solid", fgColor="E0E2E6")
_FILL_BAND = PatternFill("solid", fgColor=_GREY_SOFT)
_FILL_OK = PatternFill("solid", fgColor="E7F0E7")
_FILL_WARN = PatternFill("solid", fgColor="F7ECD2")
_FILL_ERR = PatternFill("solid", fgColor="F7E6DF")
_FILL_A = PatternFill("solid", fgColor="FBEAE3")  # giá trị bên A
_FILL_B = PatternFill("solid", fgColor="E7EEFB")  # giá trị bên B

_FONT_HEAD = Font(name="Calibri", size=11, bold=True, color=_WHITE)
_FONT_TITLE = Font(name="Calibri", size=14, bold=True, color=_NAVY)
_FONT_BOLD = Font(name="Calibri", size=11, bold=True, color=_NAVY)
_FONT_OK = Font(name="Calibri", size=11, bold=True, color="006400")
_FONT_WARN = Font(name="Calibri", size=11, bold=True, color="8A5A00")
_FONT_ERR = Font(name="Calibri", size=11, bold=True, color="AA2D00")
_FONT_MUTED = Font(name="Calibri", size=10, color="6B7280")
_FONT_LINK = Font(name="Calibri", size=11, bold=True, color="1B61C9", underline="single")
_FONT_BACK = Font(name="Calibri", size=11, bold=True, color=_NAVY, underline="single")
_FILL_BACK = PatternFill("solid", fgColor="F4D35E")  # nút back nổi bật

_SUMMARY_TITLE = "Tổng quan"

_thin = Side(style="thin", color=_GREY_LINE)
_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
_CENTER = Alignment(horizontal="center", vertical="center")
_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=False)
_RIGHT = Alignment(horizontal="right", vertical="center")

_STATUS_FILL = {"done": _FILL_OK, "warning": _FILL_WARN, "error": _FILL_ERR}
_STATUS_FONT = {"done": _FONT_OK, "warning": _FONT_WARN, "error": _FONT_ERR}


def _status_label(p) -> str:
    return {
        "done": "OK — Khớp",
        "warning": "Khác biệt",
        "error": "Lỗi",
    }.get(p.status, p.status)


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def _header_row(ws: Worksheet, row: int, headers: list[str]) -> None:
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.fill = _FILL_HEAD
        cell.font = _FONT_HEAD
        cell.alignment = _CENTER
        cell.border = _BORDER


def _safe_sheet_title(name: str, used: set[str]) -> str:
    # Excel cấm : \ / ? * [ ] và tối đa 31 ký tự, không trùng.
    clean = "".join("_" if ch in r":\/?*[]" else ch for ch in name)[:31] or "table"
    title, i = clean, 1
    while title.lower() in used:
        suffix = f"~{i}"
        title = clean[: 31 - len(suffix)] + suffix
        i += 1
    used.add(title.lower())
    return title


def _build_summary(ws: Worksheet, job, titles: dict[str, str]) -> None:
    ws.title = _SUMMARY_TITLE
    ws.sheet_view.showGridLines = False
    c = job.counters()

    ws["A1"] = "BÁO CÁO SO SÁNH DATABASE"
    ws["A1"].font = _FONT_TITLE
    ws["A2"] = f"{job.label_a} ({job.version_a})  ↔  {job.label_b} ({job.version_b})"
    ws["A2"].font = _FONT_MUTED
    ws["A3"] = (
        f"Tổng: {c['total']}   •   Khớp: {c['done']}   •   "
        f"Khác biệt: {c['warning']}   •   Lỗi: {c['error']}"
    )
    ws["A3"].font = _FONT_BOLD

    head = [
        "Bảng", "Mode", "Trạng thái",
        f"Rows {job.label_a}", f"Rows {job.label_b}", "Δ rows",
        f"Chỉ ở {job.label_a}", f"Chỉ ở {job.label_b}", "Giá trị khác",
        "Ghi chú",
    ]
    hr = 5
    _header_row(ws, hr, head)

    r = hr + 1
    for p in job.progress.values():
        # Tên bảng = hyperlink nội bộ → nhảy tới sheet của bảng đó.
        name_cell = ws.cell(row=r, column=1, value=p.name)
        title = titles.get(p.name)
        if title:
            name_cell.hyperlink = Hyperlink(
                ref=name_cell.coordinate, location=f"'{title}'!A1", display=p.name
            )
            name_cell.font = _FONT_LINK
        else:
            name_cell.font = _FONT_BOLD
        ws.cell(row=r, column=2, value=p.mode)
        st = ws.cell(row=r, column=3, value=_status_label(p))
        st.fill = _STATUS_FILL.get(p.status, _FILL_BAND)
        st.font = _STATUS_FONT.get(p.status, _FONT_BOLD)
        st.alignment = _CENTER
        ws.cell(row=r, column=4, value=p.count_a).alignment = _RIGHT
        ws.cell(row=r, column=5, value=p.count_b).alignment = _RIGHT
        dcell = ws.cell(row=r, column=6, value=p.count_delta)
        dcell.alignment = _RIGHT
        if p.count_delta != 0:
            dcell.font = _FONT_ERR
        ws.cell(row=r, column=7, value=p.only_in_a).alignment = _RIGHT
        ws.cell(row=r, column=8, value=p.only_in_b).alignment = _RIGHT
        ws.cell(row=r, column=9, value=p.value_mismatch).alignment = _RIGHT
        note = p.note
        if p.error:
            note = (note + " | " if note else "") + p.error
        ws.cell(row=r, column=10, value=note).alignment = _LEFT
        for col in range(1, 11):
            cell = ws.cell(row=r, column=col)
            cell.border = _BORDER
            if r % 2 == 0 and col != 3:
                if cell.fill.fgColor.rgb in (None, "00000000"):
                    cell.fill = _FILL_BAND
        r += 1

    widths = [34, 13, 14, 16, 16, 12, 16, 16, 14, 50]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=hr + 1, column=1)
    ws.auto_filter.ref = f"A{hr}:J{r - 1}"


def _build_table_sheet(ws: Worksheet, job, p) -> None:
    ws.sheet_view.showGridLines = False

    # Nút quay về sheet Tổng quan (ô F1 nổi bật, có hyperlink nội bộ).
    back = ws.cell(row=1, column=6, value="← Về Tổng quan")
    back.hyperlink = Hyperlink(
        ref=back.coordinate, location=f"'{_SUMMARY_TITLE}'!A1", display="← Về Tổng quan"
    )
    back.font = _FONT_BACK
    back.fill = _FILL_BACK
    back.alignment = _CENTER
    back.border = _BORDER

    ws["A1"] = p.name
    ws["A1"].font = _FONT_TITLE
    ws["A2"] = f"Mode: {p.mode}   •   Trạng thái: {_status_label(p)}"
    ws["A2"].font = _STATUS_FONT.get(p.status, _FONT_MUTED)
    if p.note:
        ws["A3"] = f"Ghi chú: {p.note}"
        ws["A3"].font = _FONT_MUTED
    if p.error:
        ws["A4"] = f"Lỗi: {p.error}"
        ws["A4"].font = _FONT_ERR

    # Khối row count
    r = 6
    ws.cell(row=r, column=1, value="Số lượng bản ghi").font = _FONT_BOLD
    r += 1
    _header_row(ws, r, ["", job.label_a, job.label_b, "Δ"])
    r += 1
    ws.cell(row=r, column=1, value="Rows").font = _FONT_BOLD
    ws.cell(row=r, column=2, value=p.count_a).alignment = _RIGHT
    ws.cell(row=r, column=3, value=p.count_b).alignment = _RIGHT
    dc = ws.cell(row=r, column=4, value=p.count_delta)
    dc.alignment = _RIGHT
    if p.count_delta:
        dc.font = _FONT_ERR
    for col in range(1, 5):
        ws.cell(row=r, column=col).border = _BORDER

    # Khối khác biệt theo id (chỉ khi không phải count-only)
    if p.mode != "count-only":
        r += 3
        ws.cell(row=r, column=1, value="Bản ghi khác biệt").font = _FONT_BOLD
        r += 1
        _header_row(ws, r, ["Loại khác biệt", "id / key", "Cột", f"Giá trị {job.label_a}", f"Giá trị {job.label_b}"])
        r += 1
        start = r

        for key in p.sample_only_a:
            ws.cell(row=r, column=1, value=f"Chỉ có ở {job.label_a}")
            ws.cell(row=r, column=2, value=str(key))
            r += 1
        for key in p.sample_only_b:
            ws.cell(row=r, column=1, value=f"Chỉ có ở {job.label_b}")
            ws.cell(row=r, column=2, value=str(key))
            r += 1
        # Cùng id, khác giá trị — chỉ rõ cột nào lệch
        if p.mismatch_details:
            for rec in p.mismatch_details:
                for d in rec["diffs"]:
                    ws.cell(row=r, column=1, value="Cùng id, khác giá trị")
                    ws.cell(row=r, column=2, value=str(rec["id"]))
                    ws.cell(row=r, column=3, value=d["col"])
                    va = ws.cell(row=r, column=4, value=d["a"])
                    vb = ws.cell(row=r, column=5, value=d["b"])
                    va.fill, vb.fill = _FILL_A, _FILL_B
                    r += 1
        elif p.sample_mismatch:  # không lấy được chi tiết cột → liệt kê id
            for key in p.sample_mismatch:
                ws.cell(row=r, column=1, value="Cùng id, khác giá trị")
                ws.cell(row=r, column=2, value=str(key))
                r += 1

        if r == start:
            ws.cell(row=r, column=1, value="Không có khác biệt theo bản ghi.").font = _FONT_OK
            r += 1
        else:
            for rr in range(start, r):
                for col in range(1, 6):
                    ws.cell(row=rr, column=col).border = _BORDER

    # Khối schema khác biệt — cột nào đổi (thêm / bỏ / đổi kiểu / nullable)
    sd = (getattr(job, "schema_tables", {}) or {}).get(p.name)
    if sd and getattr(sd, "columns", None):
        r += 3
        ws.cell(row=r, column=1, value="Schema khác biệt").font = _FONT_BOLD
        r += 1
        _header_row(ws, r, ["Cột", "Kiểu khác biệt", job.label_a, job.label_b, ""])
        r += 1
        start = r
        _KIND = {
            "added": f"thêm mới ở {job.label_b}",
            "dropped": f"bị bỏ ở {job.label_b}",
            "type_changed": "đổi kiểu",
            "nullable_changed": "đổi nullable",
            "default_changed": "đổi default",
        }
        for c in sd.columns:
            ws.cell(row=r, column=1, value=c.name)
            ws.cell(row=r, column=2, value=_KIND.get(c.kind, c.kind))
            va = ws.cell(row=r, column=3, value=(c.detail_a if c.detail_a is not None else "∅"))
            vb = ws.cell(row=r, column=4, value=(c.detail_b if c.detail_b is not None else "∅"))
            va.fill, vb.fill = _FILL_A, _FILL_B
            r += 1
        for rr in range(start, r):
            for col in range(1, 5):
                ws.cell(row=rr, column=col).border = _BORDER

    # Khối constraint khác biệt — FK / check / unique / PK
    if sd and getattr(sd, "constraints", None):
        r += 3
        ws.cell(row=r, column=1, value="Constraint khác biệt").font = _FONT_BOLD
        r += 1
        _header_row(ws, r, ["Loại", "Tên constraint", "Khác biệt", job.label_a, job.label_b])
        r += 1
        start = r
        _CKIND = {
            "added": f"thêm mới ở {job.label_b}",
            "dropped": f"bị bỏ ở {job.label_b}",
            "changed": "đổi định nghĩa",
        }
        for c in sd.constraints:
            ws.cell(row=r, column=1, value=c.type_label)
            ws.cell(row=r, column=2, value=c.name)
            ws.cell(row=r, column=3, value=_CKIND.get(c.kind, c.kind))
            va = ws.cell(row=r, column=4, value=(c.def_a if c.def_a is not None else "∅"))
            vb = ws.cell(row=r, column=5, value=(c.def_b if c.def_b is not None else "∅"))
            va.fill, vb.fill = _FILL_A, _FILL_B
            r += 1
        for rr in range(start, r):
            for col in range(1, 6):
                ws.cell(row=rr, column=col).border = _BORDER

    for i, w in enumerate([28, 26, 30, 36, 36, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_workbook(job) -> bytes:
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = _SUMMARY_TITLE

    # Tính trước tên sheet (đã sanitize/khử trùng) cho từng bảng để summary
    # biết link tới đâu trước khi tạo sheet.
    used: set[str] = {_SUMMARY_TITLE.lower()}
    titles = {p.name: _safe_sheet_title(p.name, used) for p in job.progress.values()}

    _build_summary(summary_ws, job, titles)
    for p in job.progress.values():
        ws = wb.create_sheet(titles[p.name])
        _build_table_sheet(ws, job, p)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# CSV (phẳng — 1 dòng / phát hiện)
# --------------------------------------------------------------------------- #
def build_csv(job) -> str:
    buf = io.StringIO()
    buf.write("﻿")  # BOM để Excel mở UTF-8 đúng tiếng Việt
    w = csv.writer(buf)
    w.writerow([
        "table", "mode", "status", "finding",
        "id_or_key", "column", f"value_{job.label_a}", f"value_{job.label_b}",
        "detail",
    ])
    for p in job.progress.values():
        w.writerow([
            p.name, p.mode, p.status, "summary", "", "", p.count_a, p.count_b,
            f"delta={p.count_delta}; only_a={p.only_in_a}; only_b={p.only_in_b}; "
            f"value_mismatch={p.value_mismatch}"
            + (f"; note={p.note}" if p.note else "")
            + (f"; error={p.error}" if p.error else ""),
        ])
        for key in p.sample_only_a:
            w.writerow([p.name, p.mode, p.status, "only_in_a", key, "", "", "", ""])
        for key in p.sample_only_b:
            w.writerow([p.name, p.mode, p.status, "only_in_b", key, "", "", "", ""])
        if p.mismatch_details:
            for rec in p.mismatch_details:
                for d in rec["diffs"]:
                    w.writerow([
                        p.name, p.mode, p.status, "value_mismatch",
                        rec["id"], d["col"], d["a"], d["b"], "",
                    ])
        else:
            for key in p.sample_mismatch:
                w.writerow([p.name, p.mode, p.status, "value_mismatch", key, "", "", "", ""])

        # Schema + constraint diff (lấy từ schema_tables)
        sd = (getattr(job, "schema_tables", {}) or {}).get(p.name)
        if sd:
            for c in getattr(sd, "columns", []):
                w.writerow([
                    p.name, p.mode, p.status, f"column_{c.kind}",
                    "", c.name, c.detail_a or "", c.detail_b or "", "",
                ])
            for c in getattr(sd, "constraints", []):
                w.writerow([
                    p.name, p.mode, p.status, f"constraint_{c.kind}",
                    "", c.name, c.def_a or "", c.def_b or "", c.type_label,
                ])
    return buf.getvalue()

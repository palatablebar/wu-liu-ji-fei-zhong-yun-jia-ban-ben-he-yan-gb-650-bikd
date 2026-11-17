"""M8 入口：月结页、控制数导入、预览、确认与导出。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app import config, store, web
from app.modules.m8_settlement import csv_out, service, tail

router = APIRouter(tags=["settlement"])


@router.get("/settlement", response_class=HTMLResponse)
def page_settlement(request: Request, month: str = ""):
    context = web.shell(request, active="settlement")
    run_id = context["run_id"]
    months = service.months(run_id)
    chosen = month or (months[0] if months else "")
    report = service.preview(run_id, chosen) if (run_id and chosen) else {}
    statement = service.statement(chosen) if chosen else []
    covered = {row["carrier"] for row in store.controls(chosen)} if chosen else set()
    settled = {row["carrier"] for row in statement}
    context.update({
        "month": chosen,
        "months": months,
        "report": report,
        "statement": statement,
        "exportable": bool(covered) and covered <= settled,
        "statuses": {
            "ready": tail.STATUS_READY,
            "confirmed": tail.STATUS_CONFIRMED,
            "drift": tail.STATUS_DRIFT,
            "review": tail.STATUS_REVIEW,
        },
    })
    return web.page("settlement.html", context)


@router.get("/api/settlement")
def api_settlement(run: str = "", month: str = ""):
    run_id = run or store.latest_run_id()
    chosen = month or (service.months(run_id) or [""])[0]
    return {"ok": True, "report": service.preview(run_id, chosen)}


@router.post("/api/settlement/controls")
async def api_controls(month: str = Form(...),
                       file: UploadFile | None = File(default=None),
                       sample: str = Form(default=config.SAMPLE_CONTROLS)):
    if file is not None and file.filename:
        raw = await file.read()
        name = file.filename
    else:
        name = sample
        path = config.SAMPLE_DIR / name
        raw = path.read_bytes() if path.exists() else b""
    return {"ok": True, "result": service.import_controls(raw, month, name)}


@router.post("/api/settlement/confirm")
def api_confirm(run: str = Form(default=""), month: str = Form(...),
                carriers: str = Form(default=""), force: str = Form(default="")):
    run_id = run or store.latest_run_id()
    wanted = [c.strip().upper() for c in carriers.split(",") if c.strip()] or None
    return {
        "ok": True,
        "result": service.confirm(run_id, month, wanted, force=force == "1"),
    }


@router.get("/api/settlement/statement.csv")
def api_statement_csv(month: str):
    return Response(
        content=csv_out.statement_csv(month),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="settlement-{month}.csv"'},
    )


@router.get("/api/settlement/lines.csv")
def api_lines_csv(month: str):
    return Response(
        content=csv_out.lines_csv(month),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="settlement-lines-{month}.csv"'},
    )

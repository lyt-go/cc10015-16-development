"""洞察报告 分析看板接口：汇总指标、参数运行与结果导出。

V016：新增报告版本发布（与导出批次同事务联动）与按报告编号核对导出文件
内结论版本的跨模块接口。
"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.insight_report import InsightReportService
from app.services.export_manifest import ExportManifestError

router = APIRouter(prefix="/api/insight_report", tags=["洞察报告"])
service = InsightReportService()
LIST_FIELDS = ["报告编号", "分析主题", "数据来源", "关键发现", "建议措施", "影响范围", "生成时间", "报告版本"]
STATUSES = ["草稿", "生成中", "已完成", "已归档"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=insight_report.csv"})


@router.get("/v016/cross-check")
def cross_check_manifests(report_no: str = Query(...)) -> dict[str, Any]:
    """V016 跨模块联动：读取导出文件清单中的报告编号，核对文件里的结论版本。"""
    try:
        return service.cross_check_export_manifest(report_no)
    except ExportManifestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{entry_id}/versions")
def publish_version(entry_id: int, payload: EntryPayload) -> ActionResult:
    """发布报告新版本：与导出批次在同一事务内完成版本冲突处置。"""
    new_version = str(payload.values.get("报告版本") or payload.values.get("新版本") or "").strip()
    try:
        result = service.publish_new_version(entry_id, new_version, payload.remark)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=f"报告新版本 {new_version} 已发布，已联动 {result['联动批次数']} 个导出批次",
        entry=result,
    )


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在")
    return entry

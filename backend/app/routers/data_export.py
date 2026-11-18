"""数据导出 时间线接口：返回作业时间序列结构。

V016 起在同一路由下挂载导出文件清单域接口（清单登记、片段续写、残留回滚、
留档核对、交接、下载打包、书签、版本冲突与跨模块核对），导航页与抽屉共用
同一份业务数据。
"""
from __future__ import annotations

import csv
import io
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.data_export import DataExportService
from app.services.export_manifest import ExportManifestError, ExportManifestService

router = APIRouter(prefix="/api/data_export", tags=["数据导出"])
service = DataExportService()
manifest_service = ExportManifestService()
LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段清单", "数据范围", "压缩方式", "分片大小", "导出批次"]
STATUSES = ["草稿", "导出中", "已完成", "已取消"]


def content_disposition(filename: str) -> str:
    """中文文件名按 RFC 5987 编码，避免 latin-1 无法写入响应头。"""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "export.bin"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _raise_manifest(exc: ExportManifestError, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
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
                             headers={"Content-Disposition": "attachment; filename=data_export.csv"})


# ===================== V016 导出文件清单域 =====================

@router.get("/manifests")
def list_manifests(task_no: str | None = Query(default=None)) -> dict[str, Any]:
    """导出文件清单列表：导航页与抽屉读取同一业务数据。"""
    items = manifest_service.list_manifests(task_no)
    return {"items": items, "total": len(items)}


@router.post("/manifests")
def create_manifest(payload: EntryPayload) -> ActionResult:
    """读取导出任务与洞察结论，按字段清单在同一事务内登记清单与文件记录。"""
    try:
        entry, missing = manifest_service.create_manifest(payload.values)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"导出文件清单已登记，导出批次 {entry['导出批次']}", entry=entry)


@router.get("/manifests/{manifest_id}")
def get_manifest(manifest_id: int) -> dict[str, Any]:
    try:
        return manifest_service.get_manifest_detail(manifest_id)
    except ExportManifestError as exc:
        raise _raise_manifest(exc, 404) from exc


@router.get("/manifests/{manifest_id}/resume")
def resume_plan(manifest_id: int) -> dict[str, Any]:
    """导出中断后的续写计划：只列缺失片段，已完成文件继续可下载。"""
    try:
        return manifest_service.resume_plan(manifest_id)
    except ExportManifestError as exc:
        raise _raise_manifest(exc) from exc


@router.post("/manifests/{manifest_id}/residuals")
def record_residual(manifest_id: int, payload: EntryPayload) -> ActionResult:
    try:
        record = manifest_service.record_residual(manifest_id, payload.values)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="残留已登记，等待按V016文件清单核对并整体回滚", entry=record)


@router.post("/manifests/{manifest_id}/rollback")
def rollback_manifest(manifest_id: int) -> ActionResult:
    """按文件清单记录核对并在事务中整体回滚（V016留档核对）。"""
    try:
        record = manifest_service.reconcile_and_rollback(manifest_id)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="文件清单核对完成，批次已在事务中整体回滚", entry=record)


@router.post("/manifests/{manifest_id}/archive-check")
def archive_check(manifest_id: int) -> ActionResult:
    try:
        record = manifest_service.archive_check(manifest_id)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"V016留档核对通过，留档批次号 {record['留档批次号']}", entry=record)


@router.post("/manifests/{manifest_id}/handoff")
def handoff_manifest(manifest_id: int, payload: EntryPayload) -> ActionResult:
    receiver = str(payload.values.get("接收方") or "").strip()
    try:
        record = manifest_service.handoff(manifest_id, receiver)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="V016核对无误，导出文件清单已转交接收方", entry=record)


@router.post("/manifests/{manifest_id}/version-conflict/refresh")
def refresh_latest_version(manifest_id: int) -> ActionResult:
    """最新版本策略：导出中的批次刷新为报告当前最新版本。"""
    try:
        result = manifest_service.refresh_latest_version(manifest_id)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="清单已刷新为最新报告版本与生成时间", entry=result)


@router.post("/manifests/{manifest_id}/version-conflict/followup")
def note_followup_version(manifest_id: int, payload: EntryPayload) -> ActionResult:
    """冻结版本策略：列出任务冻结之后发布的后续新版本。"""
    try:
        result = manifest_service.note_followup_version(
            manifest_id, str(payload.values.get("报告版本") or payload.values.get("版本") or ""),
        )
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="已登记后续新版本，冻结版本保持不变", entry=result)


@router.get("/manifests/{manifest_id}/version-conflict")
def version_conflict(manifest_id: int) -> dict[str, Any]:
    """判定并说明最新版本与冻结版本两种策略下的版本冲突处置。"""
    try:
        return manifest_service.check_version_conflict(manifest_id)
    except ExportManifestError as exc:
        raise _raise_manifest(exc, 404) from exc


@router.get("/manifests/{manifest_id}/package")
def package_manifest(manifest_id: int) -> StreamingResponse:
    """打包下载：包内含完整文件与导出文件清单，内容与批次、版本一致。"""
    try:
        filename, content = manifest_service.package_manifest(manifest_id)
    except ExportManifestError as exc:
        raise _raise_manifest(exc) from exc
    return StreamingResponse(
        iter([content]),
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/files/{file_id}")
def get_export_file(file_id: int) -> dict[str, Any]:
    file_row = manifest_service.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail=f"导出文件 {file_id} 不存在")
    return dict(file_row)


@router.get("/files/{file_id}/download")
def download_export_file(file_id: int) -> StreamingResponse:
    """下载文件可打开完整结论，指纹与清单记录一致才放行。"""
    try:
        filename, content = manifest_service.download_file(file_id)
    except ExportManifestError as exc:
        raise _raise_manifest(exc) from exc
    return StreamingResponse(
        iter([content]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post("/files/{file_id}/segments")
def write_segment(file_id: int, payload: EntryPayload) -> ActionResult:
    """续写片段：只写缺失片段，已写片段幂等返回，不改变同名文件内容。"""
    try:
        record = manifest_service.write_segment(file_id, payload.values)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"片段 {record['片段序号']} 已续写", entry=record)


@router.post("/files/{file_id}/bookmark")
def register_bookmark(file_id: int) -> ActionResult:
    try:
        record = manifest_service.register_bookmark(file_id)
    except ExportManifestError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="书签已登记", entry=record)


@router.get("/reports/{report_no}/cross-check")
def cross_check_report(report_no: str) -> dict[str, Any]:
    """V016 跨模块联动：按报告编号核对各批次文件内的结论版本。"""
    try:
        return manifest_service.cross_check_by_report(report_no)
    except ExportManifestError as exc:
        raise _raise_manifest(exc, 404) from exc


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="导出任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)

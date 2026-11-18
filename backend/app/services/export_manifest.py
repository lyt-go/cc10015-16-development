"""V016 数据导出：导出文件清单领域服务。

职责边界（V016 调用链）：
- 读取导出任务（data_export）与洞察结论（insight_report）两张业务数据表；
- 按字段清单整理并生成「导出文件清单」，清单记录报告版本与导出批次，可回查；
- 文件内容与导出批次在同一事务内提交，残留出现时按文件清单记录核对并整体回滚；
- 导出中断后只续写缺失片段，上一份完整文件始终可下载；
- 按 V016 留档核对，核对无误后才能交接给接收方；
- 供洞察报告 V016 模块按报告编号跨模块核对文件内的结论版本。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime
from typing import Any

from app.store import store, TransactionRollback

MANIFEST_TABLE = "export_manifest"
FILE_TABLE = "export_file"
CHECKPOINT_TABLE = "export_checkpoint"
RESIDUAL_TABLE = "export_residual"
AUDIT_TABLE = "export_audit"
HANDOFF_TABLE = "export_handoff"
INSIGHT_TABLE = "insight_report"
TASK_TABLE = "data_export"

# 导出文件清单字段（清单头），文件与批次必须同时落这些字段
MANIFEST_FIELDS = [
    "任务编号", "导出批次", "报告编号", "报告版本", "版本捕获方式", "后续新版本",
    "版本生成时间", "文件数量", "状态", "创建时间", "完成时间", "接收方",
    "留档批次号", "内容指纹", "备注",
]
# 导出文件清单中的文件记录字段
FILE_FIELDS = [
    "文件名称", "文件序号", "字段清单", "报告编号", "报告版本", "生成时间",
    "内容指纹", "字节数", "状态", "下载路径", "书签登记", "书签登记时间",
]

MANIFEST_STATUS_RUNNING = "导出中"
MANIFEST_STATUS_DONE = "已完成"
MANIFEST_STATUS_ROLLED_BACK = "已回滚"
MANIFEST_STATUS_HANDED_OFF = "已交接"
FILE_STATUS_WRITING = "续写中"
FILE_STATUS_READY = "可下载"
FILE_STATUS_ROLLED_BACK = "已回滚"

CAPTURE_LATEST = "最新版本"
CAPTURE_FROZEN = "冻结版本"
ARCHIVE_PREFIX = "V016-ARC"
BATCH_PREFIX = "BATCH-V016"
DEFAULT_TOTAL_CHUNKS = 2
# 片段粒度固定，续写时按片段序号寻址，保证不会重写已完成片段
CHUNK_SIZE = 128
# 文件内容必须在任意时刻渲染出相同字节（重复导出同名文件内容不变），
# 因此文件与批次的生成时间采用固定留档时间戳；实际操作时刻由检查点与留档记录留存。
V016_EPOCH = "2026-09-05 00:00:00"
# 渲染规则版本：写入文件记录，规则升级后旧指纹自动失效并按新规则重算
RENDER_SCHEMA_VERSION = "v016-2"

CONCLUSION_FIELDS = ["报告编号", "分析主题", "数据来源", "关键发现",
                     "建议措施", "影响范围", "生成时间", "报告版本"]


class ExportManifestError(Exception):
    """文件清单业务校验失败。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _next_serial(prefix: str, table: str, field: str) -> str:
    rows = store.rows(table)
    used = [str(r.get(field, "")) for r in rows if str(r.get(field, "")).startswith(prefix)]
    max_no = 0
    for value in used:
        tail = value[len(prefix):].lstrip("-")
        if tail.isdigit():
            max_no = max(max_no, int(tail))
    return f"{prefix}-{max_no + 1:04d}"


class ExportManifestService:
    def __init__(self) -> None:
        self._backfill_seed_fingerprints()

    # ------------------------------------------------------------------
    # 内容渲染：同一份清单记录在任何时刻渲染出的文件字节都完全一致，
    # 因此重复导出不会生成内容不同的同名文件。
    # ------------------------------------------------------------------
    def render_file_content(self, file_row: dict[str, Any],
                            manifest_row: dict[str, Any]) -> bytes:
        # 优先使用文件落盘时固化的结论快照，保证冻结版本批次不受报告后续升级影响
        snapshot = self._conclusion_snapshot(file_row, manifest_row)
        lines = [
            "# 多维分析实验平台 数据导出文件（V016）",
            f"导出批次: {manifest_row['导出批次']}",
            f"任务编号: {manifest_row['任务编号']}",
            f"报告编号: {file_row['报告编号']}",
            f"报告版本: {file_row['报告版本']}",
            f"版本捕获方式: {manifest_row['版本捕获方式']}",
            f"版本生成时间: {manifest_row['版本生成时间']}",
            f"字段清单: {file_row['字段清单']}",
            f"文件名称: {file_row['文件名称']}",
            f"生成时间: {file_row['生成时间']}",
            "",
            "## 洞察结论（完整结论，可回查报告版本与导出批次）",
        ]
        for field in CONCLUSION_FIELDS:
            lines.append(f"{field}: {snapshot.get(field, '')}")
        payload = {field: snapshot.get(field, "") for field in CONCLUSION_FIELDS}
        payload["导出批次"] = manifest_row["导出批次"]
        # 留档批次号是清单级元数据，不写入文件正文，避免留档后文件指纹漂移
        lines.extend([
            "",
            "## 字段明细（按字段清单整理，JSON 键序固定）",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "",
        ])
        return ("\n".join(lines)).encode("utf-8")

    def _conclusion_snapshot(self, file_row: dict[str, Any],
                             manifest_row: dict[str, Any]) -> dict[str, Any]:
        raw = file_row.get("结论快照")
        if raw:
            try:
                snapshot = json.loads(raw)
                if isinstance(snapshot, dict):
                    return snapshot
            except (TypeError, ValueError):
                pass
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", manifest_row["报告编号"])
        snapshot = {field: (report.get(field, "") if report else "") for field in CONCLUSION_FIELDS}
        # 种子/未落快照文件：以清单记录的结论版本为准，避免报告升级后文件自相矛盾
        snapshot["报告版本"] = file_row.get("报告版本", snapshot.get("报告版本", ""))
        return snapshot

    def _freeze_snapshot(self, file_row: dict[str, Any], manifest_row: dict[str, Any]) -> str:
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", manifest_row["报告编号"])
        snapshot = {field: (report.get(field, "") if report else "") for field in CONCLUSION_FIELDS}
        snapshot["报告版本"] = file_row["报告版本"]
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    def _backfill_seed_fingerprints(self) -> None:
        """种子数据只给了清单结构，启动时按确定性渲染补齐快照、指纹与字节数。

        以渲染规则版本为守卫：规则升级或表被重置后，旧指纹自动按新规则重算，
        避免模块级单例在表重置后跳过回填导致指纹与渲染内容漂移。
        """
        for file_row in store.rows(FILE_TABLE):
            if file_row.get("指纹规则版本") == RENDER_SCHEMA_VERSION and file_row.get("内容指纹"):
                continue
            manifest = self._require_manifest(int(file_row["manifest_id"]))
            if not file_row.get("结论快照"):
                file_row["结论快照"] = self._freeze_snapshot(file_row, manifest)
            content = self.render_file_content(file_row, manifest)
            file_row["内容指纹"] = _sha256(content)
            file_row["字节数"] = len(content)
            file_row["指纹规则版本"] = RENDER_SCHEMA_VERSION
        for manifest in store.rows(MANIFEST_TABLE):
            if manifest.get("指纹规则版本") == RENDER_SCHEMA_VERSION and manifest.get("内容指纹"):
                continue
            manifest["内容指纹"] = self._manifest_fingerprint(manifest)
            manifest["指纹规则版本"] = RENDER_SCHEMA_VERSION

    # ------------------------------------------------------------------
    # 查询：导航页与抽屉读取同一份业务数据
    # ------------------------------------------------------------------
    def list_manifests(self, task_no: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for manifest in store.rows(MANIFEST_TABLE):
            if task_no and manifest.get("任务编号") != task_no:
                continue
            items.append(self._manifest_summary(manifest))
        return items

    def _manifest_summary(self, manifest: dict[str, Any]) -> dict[str, Any]:
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"])
        checkpoints = store.filter_by_field(CHECKPOINT_TABLE, "manifest_id", manifest["id"])
        residuals = store.filter_by_field(RESIDUAL_TABLE, "manifest_id", manifest["id"])
        open_residuals = [r for r in residuals if r.get("状态") != "已处理"]
        return {
            **{k: manifest.get(k, "") for k in MANIFEST_FIELDS},
            "id": manifest["id"],
            "可下载文件数": sum(1 for f in files if f.get("状态") == FILE_STATUS_READY),
            "续写中文件数": sum(1 for f in files if f.get("状态") == FILE_STATUS_WRITING),
            "缺失片段数": sum(1 for c in checkpoints if c.get("状态") == "缺失"),
            "未处理残留数": len(open_residuals),
        }

    def get_manifest_detail(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
        checkpoints = store.filter_by_field(CHECKPOINT_TABLE, "manifest_id", manifest_id)
        audits = store.filter_by_field(AUDIT_TABLE, "manifest_id", manifest_id)
        handoffs = store.filter_by_field(HANDOFF_TABLE, "manifest_id", manifest_id)
        residuals = store.filter_by_field(RESIDUAL_TABLE, "manifest_id", manifest_id)
        return {
            "manifest": self._manifest_summary(manifest),
            "files": [dict(f) for f in files],
            "checkpoints": [dict(c) for c in checkpoints],
            "audits": [dict(a) for a in audits],
            "handoffs": [dict(h) for h in handoffs],
            "residuals": [dict(r) for r in residuals],
        }

    def get_file(self, file_id: int) -> dict[str, Any] | None:
        for row in store.rows(FILE_TABLE):
            if int(row.get("id", 0)) == file_id:
                return row
        return None

    # ------------------------------------------------------------------
    # 登记：读取导出任务与洞察结论数据表，按字段清单生成导出文件清单
    # ------------------------------------------------------------------
    def create_manifest(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["任务编号", "报告编号"]
        missing = [f for f in required if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        task_no = str(values["任务编号"]).strip()
        report_no = str(values["报告编号"]).strip()
        task = store.find_by_field(TASK_TABLE, "任务编号", task_no)
        if task is None:
            raise ExportManifestError(f"导出任务 {task_no} 不存在，无法登记导出文件清单")
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", report_no)
        if report is None:
            raise ExportManifestError(f"洞察报告 {report_no} 不存在，无法捕获结论版本")
        capture = str(values.get("版本捕获方式") or CAPTURE_LATEST).strip()
        if capture not in (CAPTURE_LATEST, CAPTURE_FROZEN):
            raise ExportManifestError("版本捕获方式只允许「最新版本」或「冻结版本」")
        try:
            file_count = max(1, int(values.get("文件数量") or 1))
        except (TypeError, ValueError):
            raise ExportManifestError("文件数量必须是正整数")
        field_list = str(values.get("字段清单") or task.get("字段清单")
                         or ",".join(CONCLUSION_FIELDS)).strip()

        batch_no = _next_serial(BATCH_PREFIX, MANIFEST_TABLE, "导出批次")
        now = _now()
        try:
            with store.transaction():
                manifest = store.insert_row(MANIFEST_TABLE, {
                    "任务编号": task_no,
                    "导出批次": batch_no,
                    "报告编号": report_no,
                    "报告版本": report.get("报告版本", ""),
                    "版本捕获方式": capture,
                    "后续新版本": "",
                    "版本生成时间": report.get("生成时间", now),
                    "文件数量": file_count,
                    "状态": MANIFEST_STATUS_RUNNING,
                    "创建时间": now,
                    "完成时间": "",
                    "接收方": "",
                    "留档批次号": "",
                    "内容指纹": "",
                    "备注": str(values.get("备注") or ""),
                })
                for seq in range(1, file_count + 1):
                    # 同名文件以批次号区分，新批次永不覆盖旧批次的可下载文件
                    name = f"{task_no}_{batch_no}_{seq:02d}_结论明细.csv"
                    store.insert_row(FILE_TABLE, {
                        "manifest_id": manifest["id"],
                        "导出批次": batch_no,
                        "文件名称": name,
                        "文件序号": seq,
                        "字段清单": field_list,
                        "报告编号": report_no,
                        "报告版本": report.get("报告版本", ""),
                        "生成时间": "",
                        "内容指纹": "",
                        "字节数": 0,
                        "状态": FILE_STATUS_WRITING,
                        "下载路径": "",
                        "书签登记": False,
                        "书签登记时间": "",
                        "总片段数": 0,
                    })
        except TransactionRollback as exc:
            raise ExportManifestError(f"清单登记事务已整体回滚：{exc}") from exc
        return self._require_manifest(int(manifest["id"])), []

    # ------------------------------------------------------------------
    # 片段续写：中断后只能续写缺失片段，已写片段幂等不重写
    # ------------------------------------------------------------------
    def write_segment(self, file_id: int, values: dict[str, Any]) -> dict[str, Any]:
        file_row = self._require_file(file_id)
        manifest = self._require_manifest(int(file_row["manifest_id"]))
        if manifest["状态"] != MANIFEST_STATUS_RUNNING:
            raise ExportManifestError(f"批次 {manifest['导出批次']} 已{manifest['状态']}，不再接受片段续写")
        if file_row["状态"] != FILE_STATUS_WRITING:
            raise ExportManifestError(f"文件 {file_row['文件名称']} 已{file_row['状态']}，禁止重写")
        try:
            chunk_no = int(values["片段序号"])
        except (TypeError, ValueError, KeyError):
            raise ExportManifestError("片段序号必须是正整数")
        if chunk_no < 1:
            raise ExportManifestError("片段序号必须从 1 开始")
        total = file_row.get("总片段数") or 0
        if values.get("总片段数"):
            try:
                total = int(values["总片段数"])
            except (TypeError, ValueError):
                raise ExportManifestError("总片段数必须是正整数")
            file_row["总片段数"] = total
        if total and chunk_no > total:
            raise ExportManifestError(f"片段序号 {chunk_no} 超出总片段数 {total}")
        bytes_written = int(values.get("字节数") or CHUNK_SIZE)

        existing = None
        for cp in store.filter_by_field(CHECKPOINT_TABLE, "file_id", file_id):
            if int(cp.get("片段序号", 0)) == chunk_no:
                existing = cp
                break
        with store.transaction():
            if existing is not None and existing.get("状态") == "已写":
                # 幂等：同一片段重复提交直接返回既有检查点，不产生任何内容差异
                checkpoint = existing
            else:
                if existing is not None:
                    existing["状态"] = "已写"
                    existing["已写字节"] = bytes_written
                    existing["更新时间"] = _now()
                    checkpoint = existing
                else:
                    checkpoint = store.insert_row(CHECKPOINT_TABLE, {
                        "manifest_id": manifest["id"],
                        "file_id": file_id,
                        "片段序号": chunk_no,
                        "片段范围": f"{(chunk_no - 1) * CHUNK_SIZE}-{chunk_no * CHUNK_SIZE}",
                        "已写字节": bytes_written,
                        "状态": "已写",
                        "更新时间": _now(),
                    })
            self._maybe_finalize(file_row, manifest)
        return dict(checkpoint)

    def _maybe_finalize(self, file_row: dict[str, Any], manifest: dict[str, Any]) -> None:
        total = int(file_row.get("总片段数") or 0)
        if total <= 0:
            return
        written = {int(c["片段序号"]) for c in
                   store.filter_by_field(CHECKPOINT_TABLE, "file_id", file_row["id"])
                   if c.get("状态") == "已写"}
        if not all(i in written for i in range(1, total + 1)):
            return
        # 片段齐全：先固化结论快照与固定生成时间，再渲染确定性内容，包体此后不可变
        file_row["结论快照"] = self._freeze_snapshot(file_row, manifest)
        file_row["生成时间"] = file_row.get("生成时间") or V016_EPOCH
        content = self.render_file_content(file_row, manifest)
        file_row["状态"] = FILE_STATUS_READY
        file_row["内容指纹"] = _sha256(content)
        file_row["字节数"] = len(content)
        file_row["指纹规则版本"] = RENDER_SCHEMA_VERSION
        file_row["下载路径"] = f"/api/data_export/files/{file_row['id']}/download"
        # 完整文件同时登记书签，供下载、打包与书签登记共用
        file_row["书签登记"] = True
        file_row["书签登记时间"] = file_row.get("书签登记时间") or _now()
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"])
        if all(f.get("状态") == FILE_STATUS_READY for f in files):
            manifest["状态"] = MANIFEST_STATUS_DONE
            manifest["完成时间"] = _now()
            manifest["内容指纹"] = self._manifest_fingerprint(manifest)

    def resume_plan(self, manifest_id: int) -> dict[str, Any]:
        """返回缺失片段清单；上一份完整文件保持可下载，不在续写范围内。"""
        manifest = self._require_manifest(manifest_id)
        if manifest["状态"] != MANIFEST_STATUS_RUNNING:
            raise ExportManifestError(f"批次 {manifest['导出批次']} 状态为{manifest['状态']}，无需续写")
        plan = []
        for file_row in store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id):
            written = {int(c["片段序号"]) for c in
                       store.filter_by_field(CHECKPOINT_TABLE, "file_id", file_row["id"])
                       if c.get("状态") == "已写"}
            total = int(file_row.get("总片段数") or 0)
            missing = [i for i in range(1, total + 1) if i not in written]
            plan.append({
                "file_id": file_row["id"],
                "文件名称": file_row["文件名称"],
                "状态": file_row["状态"],
                "总片段数": total,
                "已写片段": sorted(written),
                "缺失片段": missing,
                "仅续写缺失片段": file_row["状态"] == FILE_STATUS_WRITING and bool(missing),
            })
        return {"导出批次": manifest["导出批次"], "续写计划": plan}

    # ------------------------------------------------------------------
    # 残留与回滚：按文件清单记录核对，事务内整体回滚
    # ------------------------------------------------------------------
    def record_residual(self, manifest_id: int, values: dict[str, Any]) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        description = str(values.get("残留说明") or values.get("备注") or "").strip()
        if not description:
            raise ExportManifestError("登记残留必须填写残留说明")
        with store.transaction():
            residual = store.insert_row(RESIDUAL_TABLE, {
                "manifest_id": manifest_id,
                "导出批次": manifest["导出批次"],
                "残留说明": description,
                "发现时间": _now(),
                "状态": "待处理",
            })
        return dict(residual)

    def reconcile_and_rollback(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        residuals = [r for r in store.filter_by_field(RESIDUAL_TABLE, "manifest_id", manifest_id)
                     if r.get("状态") != "已处理"]
        if not residuals:
            raise ExportManifestError("没有残留记录，不允许执行整体回滚")
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
        # 先按文件清单记录核对：文件数与归属必须与清单一致，否则事务直接回滚
        if len(files) != int(manifest.get("文件数量", 0)) or \
                any(int(f.get("manifest_id", 0)) != manifest_id for f in files):
            with store.transaction():
                raise TransactionRollback("文件清单记录核对不一致，回滚整批导出")
        audit_detail = (
            f"发现残留 {len(residuals)} 条；按V016文件清单记录核对 {len(files)} 个文件，"
            "核对后在事务中整体回滚，批次内文件全部停止下载"
        )
        with store.transaction():
            for file_row in files:
                file_row["状态"] = FILE_STATUS_ROLLED_BACK
                file_row["下载路径"] = ""
                file_row["书签登记"] = False
            for cp in store.filter_by_field(CHECKPOINT_TABLE, "manifest_id", manifest_id):
                cp["状态"] = "已回滚"
            for residual in residuals:
                residual["状态"] = "已处理"
            manifest["状态"] = MANIFEST_STATUS_ROLLED_BACK
            audit = store.insert_row(AUDIT_TABLE, {
                "manifest_id": manifest_id,
                "留档批次号": manifest.get("留档批次号", ""),
                "核对项": "V016留档核对-残留整体回滚",
                "核对结果": "已回滚",
                "核对时间": _now(),
                "详情": audit_detail,
            })
        return dict(audit)

    # ------------------------------------------------------------------
    # V016 留档核对与交接
    # ------------------------------------------------------------------
    def archive_check(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        if manifest["状态"] != MANIFEST_STATUS_DONE:
            raise ExportManifestError(f"批次 {manifest['导出批次']} 尚未完成，无法按V016留档核对")
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
        failures: list[str] = []
        for file_row in files:
            if file_row.get("状态") != FILE_STATUS_READY:
                failures.append(f"文件 {file_row['文件名称']} 不可下载")
                continue
            content = self.render_file_content(file_row, manifest)
            if file_row.get("内容指纹") != _sha256(content):
                failures.append(f"文件 {file_row['文件名称']} 内容指纹与清单记录不一致")
            if file_row.get("报告版本") != manifest.get("报告版本"):
                failures.append(f"文件 {file_row['文件名称']} 结论版本与清单报告版本不一致")
            text = content.decode("utf-8", errors="replace")
            if manifest["导出批次"] not in text or str(manifest["报告版本"]) not in text:
                failures.append(f"文件 {file_row['文件名称']} 缺少批次号或版本说明")
        if failures:
            with store.transaction():
                audit = store.insert_row(AUDIT_TABLE, {
                    "manifest_id": manifest_id,
                    "留档批次号": "",
                    "核对项": "V016留档核对",
                    "核对结果": "不通过",
                    "核对时间": _now(),
                    "详情": "；".join(failures),
                })
            raise ExportManifestError("V016留档核对不通过：" + "；".join(failures))
        with store.transaction():
            archive_no = _next_serial(ARCHIVE_PREFIX, AUDIT_TABLE, "留档批次号")
            manifest["留档批次号"] = archive_no
            audit = store.insert_row(AUDIT_TABLE, {
                "manifest_id": manifest_id,
                "留档批次号": archive_no,
                "核对项": "V016留档核对",
                "核对结果": "通过",
                "核对时间": _now(),
                "详情": f"清单 {len(files)} 个文件全部可下载，报告版本与文件内结论版本一致，指纹核对通过",
            })
        return dict(audit)

    def handoff(self, manifest_id: int, receiver: str) -> dict[str, Any]:
        receiver = (receiver or "").strip()
        if not receiver:
            raise ExportManifestError("交接必须指定接收方")
        manifest = self._require_manifest(manifest_id)
        passed = [a for a in store.filter_by_field(AUDIT_TABLE, "manifest_id", manifest_id)
                  if a.get("核对项") == "V016留档核对" and a.get("核对结果") == "通过"]
        if not passed or not manifest.get("留档批次号"):
            raise ExportManifestError("V016留档核对未通过，禁止转交导出文件清单")
        files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
        with store.transaction():
            record = store.insert_row(HANDOFF_TABLE, {
                "manifest_id": manifest_id,
                "接收方": receiver,
                "交接时间": _now(),
                "清单文件数": len(files),
                "留档批次号": manifest["留档批次号"],
                "核对结果": "通过",
                "备注": "V016核对无误后转交接收方的导出文件清单",
            })
            manifest["状态"] = MANIFEST_STATUS_HANDED_OFF
            manifest["接收方"] = receiver
        return dict(record)

    # ------------------------------------------------------------------
    # 下载、打包、书签
    # ------------------------------------------------------------------
    def download_file(self, file_id: int) -> tuple[str, bytes]:
        file_row = self._require_file(file_id)
        manifest = self._require_manifest(int(file_row["manifest_id"]))
        if file_row.get("状态") != FILE_STATUS_READY:
            raise ExportManifestError(f"文件 {file_row['文件名称']} 当前状态 {file_row.get('状态')}，不可下载")
        content = self.render_file_content(file_row, manifest)
        if file_row.get("内容指纹") != _sha256(content):
            raise ExportManifestError("文件内容指纹与清单记录不符，已拦截下载")
        return str(file_row["文件名称"]), content

    def package_manifest(self, manifest_id: int) -> tuple[str, bytes]:
        manifest = self._require_manifest(manifest_id)
        if manifest["状态"] not in (MANIFEST_STATUS_DONE, MANIFEST_STATUS_HANDED_OFF):
            raise ExportManifestError("只有留档核对完成的批次才能打包下载")
        files = sorted(
            [f for f in store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
             if f.get("状态") == FILE_STATUS_READY],
            key=lambda f: int(f["文件序号"]),
        )
        if not files:
            raise ExportManifestError("清单内没有可下载文件")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_row in files:
                info = zipfile.ZipInfo(file_row["文件名称"], date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, self.render_file_content(file_row, manifest))
            manifest_json = json.dumps(
                self._manifest_summary(manifest), ensure_ascii=False, sort_keys=True, indent=2,
            ).encode("utf-8")
            info = zipfile.ZipInfo("导出文件清单.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, manifest_json)
        name = f"{manifest['任务编号']}_{manifest['导出批次']}_导出包.zip"
        return name, buf.getvalue()

    def register_bookmark(self, file_id: int) -> dict[str, Any]:
        file_row = self._require_file(file_id)
        if file_row.get("状态") != FILE_STATUS_READY:
            raise ExportManifestError("只有可下载的完整文件才能登记书签")
        with store.transaction():
            file_row["书签登记"] = True
            file_row["书签登记时间"] = file_row.get("书签登记时间") or _now()
        return dict(file_row)

    # ------------------------------------------------------------------
    # 报告版本冲突：最新版本 vs 任务冻结时捕获的版本
    # ------------------------------------------------------------------
    def check_version_conflict(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", manifest["报告编号"])
        current = str(report.get("报告版本", "")) if report else ""
        followups = [v for v in str(manifest.get("后续新版本", "")).split(",") if v]
        if current and current != manifest["报告版本"] and current not in followups:
            followups.append(current)
        # 报告当前版本已变更，或冻结之后已登记后续新版本，都视为存在版本冲突
        has_conflict = current != manifest["报告版本"] or bool(followups)
        return {
            "导出批次": manifest["导出批次"],
            "报告编号": manifest["报告编号"],
            "版本捕获方式": manifest["版本捕获方式"],
            "清单记录版本": manifest["报告版本"],
            "清单记录生成时间": manifest["版本生成时间"],
            "报告当前版本": current,
            "后续新版本": followups,
            "存在冲突": has_conflict,
            "处置说明": self._conflict_policy(manifest["版本捕获方式"]),
        }

    @staticmethod
    def _conflict_policy(capture: str) -> str:
        if capture == CAPTURE_LATEST:
            return "清单记录最新报告版本和生成时间；导出中可刷新到当前最新版本，完成后包体不可变"
        return "清单记录任务冻结时捕获的版本，并列出冻结之后发布的后续新版本"

    def note_followup_version(self, manifest_id: int, version: str) -> dict[str, Any]:
        version = (version or "").strip()
        if not version:
            raise ExportManifestError("后续新版本号不能为空")
        manifest = self._require_manifest(manifest_id)
        with store.transaction():
            followups = [v for v in str(manifest.get("后续新版本", "")).split(",") if v]
            if version not in followups and version != manifest["报告版本"]:
                followups.append(version)
            manifest["后续新版本"] = ",".join(followups)
        return self.check_version_conflict(manifest_id)

    def refresh_latest_version(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._require_manifest(manifest_id)
        if manifest["版本捕获方式"] != CAPTURE_LATEST:
            raise ExportManifestError("冻结版本批次不允许刷新为最新版本，只能登记后续新版本")
        if manifest["状态"] != MANIFEST_STATUS_RUNNING:
            raise ExportManifestError("批次已完成或已交接，文件包体不可变，只能查看版本冲突")
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", manifest["报告编号"])
        if report is None:
            raise ExportManifestError("洞察结论已不存在，无法刷新版本")
        with store.transaction():
            manifest["报告版本"] = report.get("报告版本", "")
            manifest["版本生成时间"] = report.get("生成时间", _now())
            for file_row in store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id):
                if file_row.get("状态") == FILE_STATUS_WRITING:
                    file_row["报告版本"] = manifest["报告版本"]
        return self.check_version_conflict(manifest_id)

    def on_report_version_changed(self, report_no: str, old_version: str,
                                  new_version: str, generated_at: str) -> None:
        """洞察报告发布新版本时的跨模块联动入口（在同一事务内处理）。"""
        for manifest in store.rows(MANIFEST_TABLE):
            if manifest.get("报告编号") != report_no:
                continue
            followups = [v for v in str(manifest.get("后续新版本", "")).split(",") if v]
            if new_version not in followups and new_version != manifest["报告版本"]:
                followups.append(new_version)
            if manifest.get("状态") == MANIFEST_STATUS_RUNNING and \
                    manifest.get("版本捕获方式") == CAPTURE_LATEST:
                manifest["报告版本"] = new_version
                manifest["版本生成时间"] = generated_at
                for file_row in store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"]):
                    if file_row.get("状态") == FILE_STATUS_WRITING:
                        file_row["报告版本"] = new_version
                # 已生成片段的文件无法追赶版本，保留为后续新版本冲突项
                ready = [f for f in store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"])
                         if f.get("状态") == FILE_STATUS_READY]
                if ready:
                    manifest["后续新版本"] = ",".join(followups)
            else:
                # 冻结版本或已完成/已交接：包体不可变，只列出后续新版本
                manifest["后续新版本"] = ",".join(followups)

    # ------------------------------------------------------------------
    # 跨模块联动：洞察报告 V016 模块按报告编号核对文件内结论版本
    # ------------------------------------------------------------------
    def cross_check_by_report(self, report_no: str) -> dict[str, Any]:
        report = store.find_by_field(INSIGHT_TABLE, "报告编号", report_no)
        if report is None:
            raise ExportManifestError(f"洞察报告 {report_no} 不存在")
        checks = []
        for manifest in store.rows(MANIFEST_TABLE):
            if manifest.get("报告编号") != report_no:
                continue
            file_checks = []
            for file_row in store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"]):
                embedded_ok = file_row.get("报告版本") == manifest.get("报告版本")
                fingerprint_ok = True
                if file_row.get("状态") == FILE_STATUS_READY:
                    content = self.render_file_content(file_row, manifest)
                    fingerprint_ok = file_row.get("内容指纹") == _sha256(content)
                file_checks.append({
                    "file_id": file_row["id"],
                    "文件名称": file_row["文件名称"],
                    "状态": file_row["状态"],
                    "文件内结论版本": file_row.get("报告版本"),
                    "清单报告版本": manifest.get("报告版本"),
                    "版本一致": embedded_ok,
                    "指纹一致": fingerprint_ok,
                })
            checks.append({
                "manifest_id": manifest["id"],
                "导出批次": manifest["导出批次"],
                "批次状态": manifest["状态"],
                "留档批次号": manifest.get("留档批次号", ""),
                "文件核对": file_checks,
                "整体通过": bool(file_checks) and all(c["版本一致"] and c["指纹一致"] for c in file_checks),
            })
        return {
            "报告编号": report_no,
            "报告当前版本": report.get("报告版本", ""),
            "核对批次数量": len(checks),
            "核对结果": checks,
            "全部通过": all(c["整体通过"] for c in checks) if checks else True,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _manifest_fingerprint(self, manifest: dict[str, Any]) -> str:
        files = sorted(store.filter_by_field(FILE_TABLE, "manifest_id", manifest["id"]),
                       key=lambda f: int(f["文件序号"]))
        joined = "|".join(str(f.get("内容指纹", "")) for f in files)
        return _sha256(joined.encode("utf-8"))

    def _require_manifest(self, manifest_id: int) -> dict[str, Any]:
        manifest = store.find(MANIFEST_TABLE, manifest_id)
        if manifest is None:
            raise ExportManifestError(f"导出文件清单 {manifest_id} 不存在")
        return manifest

    def _require_file(self, file_id: int) -> dict[str, Any]:
        file_row = self.get_file(file_id)
        if file_row is None:
            raise ExportManifestError(f"导出文件 {file_id} 不存在")
        return file_row

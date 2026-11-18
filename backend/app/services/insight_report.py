"""洞察报告 分析看板服务：汇总指标、运行分析并生成模拟结果。

V016 起承担跨模块联动职责：
- 报告发布新版本时，在同一事务内通知导出文件清单域处理版本冲突；
- 提供按报告编号读取导出文件清单、核对文件内结论版本的入口。
"""
from __future__ import annotations

import random
from typing import Any

from app.store import store
from app.services.export_manifest import (
    CAPTURE_FROZEN,
    CAPTURE_LATEST,
    ExportManifestError,
    ExportManifestService,
)

MODULE = "insight_report"
REQUIRED_FIELDS = ["报告编号", "分析主题", "数据来源"]
STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
ACTION_RULES = {"开始生成": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []

# 跨模块核对与版本联动复用同一个导出文件清单服务实例
manifest_service = ExportManifestService()


class InsightReportService:
    def list_results(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["指标值"] = round(random.uniform(0.65, 0.98), 3)
            d["对比基准"] = round(random.uniform(0.60, 0.92), 3)
            d["差异"] = round(d["指标值"] - d["对比基准"], 3)
            results.append(d)
        return results

    def list_reports(self) -> list[dict[str, Any]]:
        """供数据导出侧读取的稳定结论列表（不含随机指标）。"""
        return [dict(row) for row in store.rows(MODULE)]

    def get_report_by_no(self, report_no: str) -> dict[str, Any] | None:
        return store.find_by_field(MODULE, "报告编号", report_no)

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        results: list[dict] = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i%3)+1}",
                "指标值": round(random.uniform(0.62, 1.02), 3),
                "对比基准": round(random.uniform(0.58, 0.95), 3),
                "差异": round(random.uniform(-0.2, 0.3), 3),
            })
        return results

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def publish_new_version(self, entry_id: int, new_version: str,
                            remark: str | None = None) -> dict[str, Any]:
        """发布报告新版本，并在同一事务内完成导出文件清单的版本冲突处理。

        - 最新版本批次（导出中）：清单记录刷新为最新报告版本和生成时间；
        - 冻结版本或已完成/已交接批次：保留冻结版本，列出后续新版本。
        """
        from datetime import datetime
        report = store.find(MODULE, entry_id)
        if report is None:
            raise ExportManifestError(f"洞察报告 {entry_id} 不存在")
        new_version = (new_version or "").strip()
        if not new_version:
            raise ExportManifestError("新版本号不能为空")
        old_version = str(report.get("报告版本", ""))
        if new_version == old_version:
            raise ExportManifestError("新版本号与当前版本相同，无需发布")
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with store.transaction():
            report["报告版本"] = new_version
            report["生成时间"] = generated_at
            report["status"] = STATUS_ORDER[2]
            store.bump_version(MODULE)
            # 与导出批次在同一事务内联动，避免清单引用到没有版本说明的旧包
            manifest_service.on_report_version_changed(
                str(report.get("报告编号", "")), old_version, new_version, generated_at,
            )
            affected = store.filter_by_field("export_manifest", "报告编号", report.get("报告编号"))
            result = {
                "报告编号": report.get("报告编号"),
                "旧版本": old_version,
                "新版本": new_version,
                "生成时间": generated_at,
                "联动批次数": len(affected),
                "联动明细": [
                    {
                        "导出批次": m.get("导出批次"),
                        "版本捕获方式": m.get("版本捕获方式"),
                        "清单记录版本": m.get("报告版本"),
                        "后续新版本": m.get("后续新版本", ""),
                        "处置": ("刷新为最新版本"
                                 if m.get("版本捕获方式") == CAPTURE_LATEST
                                 and m.get("状态") == "导出中"
                                 else "保留冻结/已完成版本并登记后续新版本"
                                 if m.get("版本捕获方式") == CAPTURE_FROZEN
                                 or m.get("状态") != "导出中"
                                 else "记录后续新版本"),
                    }
                    for m in affected
                ],
                "备注": remark or "",
            }
        return result

    def cross_check_export_manifest(self, report_no: str) -> dict[str, Any]:
        """V016 跨模块联动：读取导出文件清单中的报告编号，核对文件里的结论版本。"""
        return manifest_service.cross_check_by_report(report_no)

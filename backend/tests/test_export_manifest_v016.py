"""V016 数据导出文件清单域测试：覆盖清单登记、片段续写、确定性下载、
残留整体回滚、V016留档核对、报告版本冲突处置与跨模块核对交接全链路。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS
from app.store import store, TransactionRollback
from app.services.export_manifest import (
    MANIFEST_TABLE, FILE_TABLE, CHECKPOINT_TABLE, RESIDUAL_TABLE,
    AUDIT_TABLE, HANDOFF_TABLE, INSIGHT_TABLE, TASK_TABLE,
    CAPTURE_FROZEN, CAPTURE_LATEST,
    FILE_STATUS_READY, FILE_STATUS_WRITING, FILE_STATUS_ROLLED_BACK,
    MANIFEST_STATUS_RUNNING, MANIFEST_STATUS_DONE, MANIFEST_STATUS_ROLLED_BACK,
    MANIFEST_STATUS_HANDED_OFF,
    ExportManifestError, ExportManifestService,
)
from app.services.insight_report import InsightReportService

EXPORT_TABLES = [MANIFEST_TABLE, FILE_TABLE, CHECKPOINT_TABLE,
                 RESIDUAL_TABLE, AUDIT_TABLE, HANDOFF_TABLE]


@pytest.fixture(autouse=True)
def isolated_tables():
    """每个用例前后都把相关表恢复到原始种子状态，避免全局单例与其他模块串数据。

    旧管线测试会清空全部表，因此快照必须直接取自 SEED_ROWS，并在用例开始前
    就完成恢复，不能依赖进入用例时的当前表状态。
    """
    seed_snapshot = {t: [dict(r) for r in SEED_ROWS[t]]
                     for t in EXPORT_TABLES + [INSIGHT_TABLE, TASK_TABLE]}
    for table, rows in seed_snapshot.items():
        store._tables[table] = [dict(r) for r in rows]
    yield
    for table, rows in seed_snapshot.items():
        store._tables[table] = [dict(r) for r in rows]


@pytest.fixture
def svc():
    return ExportManifestService()


def _fresh_manifest(svc: ExportManifestService, capture: str = CAPTURE_LATEST,
                    file_count: int = 2, task_no: str = "DATA-0001",
                    report_no: str = "INSI-0001") -> dict:
    entry, missing = svc.create_manifest({
        "任务编号": task_no, "报告编号": report_no,
        "版本捕获方式": capture, "文件数量": file_count,
    })
    assert not missing
    return entry


def _complete_all_segments(svc: ExportManifestService, manifest_id: int, total: int = 2):
    files = store.filter_by_field(FILE_TABLE, "manifest_id", manifest_id)
    for file_row in files:
        for chunk in range(1, total + 1):
            svc.write_segment(file_row["id"], {"片段序号": chunk, "总片段数": total})


def test_seed_fingerprints_backfilled_and_manifest_list(svc):
    """种子清单可直接读取，文件指纹与内容可回查。"""
    items = svc.list_manifests()
    assert len(items) == 2
    assert items[0]["导出批次"] == "BATCH-V016-0001"
    assert items[0]["可下载文件数"] == 2
    for file_row in store.rows(FILE_TABLE):
        assert file_row["内容指纹"]
        assert file_row["字节数"] > 0
    detail = svc.get_manifest_detail(1)
    assert {f["状态"] for f in detail["files"]} == {FILE_STATUS_READY}


def test_create_manifest_reads_task_and_report_in_single_transaction(svc):
    """清单与文件记录同事务提交，字段含报告版本与导出批次，可回查。"""
    before = len(store.rows(MANIFEST_TABLE))
    entry = _fresh_manifest(svc)
    assert len(store.rows(MANIFEST_TABLE)) == before + 1
    assert entry["导出批次"] == "BATCH-V016-0003"
    assert entry["报告版本"] == "v1.0"
    assert entry["状态"] == MANIFEST_STATUS_RUNNING
    files = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])
    assert len(files) == 2
    for f in files:
        assert entry["导出批次"] in f["文件名称"]
        assert f["报告编号"] == "INSI-0001"
        assert f["状态"] == FILE_STATUS_WRITING
    # 不存在的任务/报告直接拒绝，不产生任何残留记录
    with pytest.raises(ExportManifestError):
        svc.create_manifest({"任务编号": "NOPE", "报告编号": "INSI-0001"})
    assert len(store.rows(MANIFEST_TABLE)) == before + 1
    with pytest.raises(ExportManifestError):
        svc.create_manifest({"任务编号": "DATA-0001", "报告编号": "NOPE"})


def test_segment_write_is_idempotent_and_resume_only_lists_missing(svc):
    """中断后只能续写缺失片段，已写片段重复提交不产生差异。"""
    entry = _fresh_manifest(svc, file_count=2)
    file_id = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])[0]["id"]
    svc.write_segment(file_id, {"片段序号": 1, "总片段数": 2})
    again = svc.write_segment(file_id, {"片段序号": 1, "总片段数": 2})
    checkpoints = store.filter_by_field(CHECKPOINT_TABLE, "file_id", file_id)
    assert len([c for c in checkpoints if c["片段序号"] == 1]) == 1
    assert again["状态"] == "已写"

    plan = svc.resume_plan(entry["id"])
    target = next(p for p in plan["续写计划"] if p["file_id"] == file_id)
    assert target["缺失片段"] == [2]
    assert target["仅续写缺失片段"] is True
    # 上一份完整文件（种子批次）始终可下载
    name, content = svc.download_file(1)
    assert name and content

    with pytest.raises(ExportManifestError):
        svc.write_segment(file_id, {"片段序号": 3, "总片段数": 2})


def test_completion_makes_files_downloadable_with_full_conclusion(svc):
    """片段齐全后文件可下载，内容含完整结论、报告版本与导出批次。"""
    entry = _fresh_manifest(svc, file_count=1)
    file_row = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])[0]
    _complete_all_segments(svc, entry["id"], total=2)
    refreshed = svc.get_file(file_row["id"])
    assert refreshed["状态"] == FILE_STATUS_READY
    assert refreshed["书签登记"] is True  # 完整文件自动登记书签
    name, content = svc.download_file(refreshed["id"])
    text = content.decode("utf-8")
    assert entry["导出批次"] in text
    assert "报告版本: v1.0" in text
    assert "INSI-0001" in text
    assert "关键发现" in text
    assert store.find(MANIFEST_TABLE, entry["id"])["状态"] == MANIFEST_STATUS_DONE


def test_repeated_export_never_changes_same_named_file_content(svc):
    """重复导出/重复下载同一份文件，字节与指纹完全一致，且批次号隔离文件名。"""
    entry = _fresh_manifest(svc, file_count=1)
    file_row = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])[0]
    _complete_all_segments(svc, entry["id"])
    name1, c1 = svc.download_file(file_row["id"])
    name2, c2 = svc.download_file(file_row["id"])
    assert name1 == name2 and c1 == c2
    assert svc.get_file(file_row["id"])["内容指纹"] == __import__("hashlib").sha256(c1).hexdigest()
    # 再登记一次重复导出：批次号不同，不会覆盖旧文件
    second = _fresh_manifest(svc, file_count=1)
    second_file = store.filter_by_field(FILE_TABLE, "manifest_id", second["id"])[0]
    assert second_file["文件名称"] != name1
    _complete_all_segments(svc, second["id"])
    _, c3 = svc.download_file(second_file["id"])
    assert svc.get_file(second_file["id"])["内容指纹"] == __import__("hashlib").sha256(c3).hexdigest()


def test_residual_triggers_manifest_reconcile_and_full_rollback(svc):
    """残留出现时按文件清单记录核对，并在事务中整体回滚。"""
    entry = _fresh_manifest(svc, file_count=2)
    # 先完成一个文件，另一个只写一半，模拟中断后出现残留
    files = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])
    svc.write_segment(files[0]["id"], {"片段序号": 1, "总片段数": 2})
    svc.write_segment(files[0]["id"], {"片段序号": 2, "总片段数": 2})
    svc.write_segment(files[1]["id"], {"片段序号": 1, "总片段数": 2})
    svc.record_residual(entry["id"], {"残留说明": "分片2写入失败"})
    audit = svc.reconcile_and_rollback(entry["id"])
    assert audit["核对结果"] == "已回滚"
    rolled = store.find(MANIFEST_TABLE, entry["id"])
    assert rolled["状态"] == MANIFEST_STATUS_ROLLED_BACK
    for f in store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"]):
        assert f["状态"] == FILE_STATUS_ROLLED_BACK
        assert f["下载路径"] == ""
        assert f["书签登记"] is False
        with pytest.raises(ExportManifestError):
            svc.download_file(f["id"])
    # 残留已处理，重复回滚被拒绝
    with pytest.raises(ExportManifestError):
        svc.reconcile_and_rollback(entry["id"])
    # 上一份完整文件（种子批次1）继续可下载
    _, prev = svc.download_file(1)
    assert prev


def test_transaction_rolls_back_all_tables_on_failure(svc):
    """事务内异常必须整体恢复，清单与批次不会半提交。"""
    manifest_rows = len(store.rows(MANIFEST_TABLE))
    file_rows = len(store.rows(FILE_TABLE))
    with pytest.raises(TransactionRollback):
        with store.transaction():
            store.insert_row(MANIFEST_TABLE, {"任务编号": "X", "导出批次": "BATCH-TX-0001",
                                              "状态": MANIFEST_STATUS_RUNNING})
            store.insert_row(FILE_TABLE, {"文件名称": "x.csv", "manifest_id": 999})
            raise TransactionRollback("模拟核对失败")
    assert len(store.rows(MANIFEST_TABLE)) == manifest_rows
    assert len(store.rows(FILE_TABLE)) == file_rows


def test_v016_archive_check_and_handoff_flow(svc):
    """V016留档核对通过后才能交接，交接记录含留档批次号与接收方。"""
    entry = _fresh_manifest(svc, file_count=2)
    _complete_all_segments(svc, entry["id"])
    # 未留档核对不能交接
    with pytest.raises(ExportManifestError):
        svc.handoff(entry["id"], "接收方V016")
    audit = svc.archive_check(entry["id"])
    assert audit["核对结果"] == "通过"
    assert audit["留档批次号"].startswith("V016-ARC")
    handoff = svc.handoff(entry["id"], "接收方V016")
    assert handoff["接收方"] == "接收方V016"
    assert handoff["留档批次号"] == audit["留档批次号"]
    assert handoff["清单文件数"] == 2
    assert store.find(MANIFEST_TABLE, entry["id"])["状态"] == MANIFEST_STATUS_HANDED_OFF
    # 打包内容含完整文件与导出文件清单，且可重复打包结果一致
    name1, zip1 = svc.package_manifest(entry["id"])
    _, zip2 = svc.package_manifest(entry["id"])
    assert zip1 == zip2
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip1)) as zf:
        assert "导出文件清单.json" in zf.namelist()
        assert len(zf.namelist()) == 3


def test_version_conflict_frozen_capture_lists_followups(svc):
    """冻结版本：清单记录冻结版本，报告升级只列后续新版本，包体不可变。"""
    entry = _fresh_manifest(svc, capture=CAPTURE_FROZEN, file_count=1)
    file_row = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])[0]
    _complete_all_segments(svc, entry["id"])
    _, before = svc.download_file(file_row["id"])

    conflict = svc.note_followup_version(entry["id"], "v3.0")
    assert conflict["存在冲突"] is True
    assert conflict["清单记录版本"] == "v1.0"
    assert "v3.0" in conflict["后续新版本"]
    assert "冻结" in conflict["处置说明"]
    # 冻结批次禁止刷新为最新版本
    with pytest.raises(ExportManifestError):
        svc.refresh_latest_version(entry["id"])
    _, after = svc.download_file(file_row["id"])
    assert before == after  # 旧包字节不变，用户不会下载到不同内容的同名文件


def test_version_conflict_latest_capture_refreshes_while_running(svc):
    """最新版本：导出中的批次刷新为最新报告版本和生成时间。"""
    entry = _fresh_manifest(svc, capture=CAPTURE_LATEST, file_count=2)
    report_svc = InsightReportService()
    report_svc.publish_new_version(1, "v1.5")
    conflict = svc.refresh_latest_version(entry["id"])
    assert conflict["清单记录版本"] == "v1.5"
    assert conflict["存在冲突"] is False
    files = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])
    assert all(f["报告版本"] == "v1.5" for f in files)
    # 完成后包体不可变，再发布版本只能登记冲突
    _complete_all_segments(svc, entry["id"])
    report_svc.publish_new_version(1, "v1.6")
    with pytest.raises(ExportManifestError):
        svc.refresh_latest_version(entry["id"])
    done_conflict = svc.check_version_conflict(entry["id"])
    assert done_conflict["清单记录版本"] == "v1.5"
    assert "v1.6" in done_conflict["后续新版本"]


def test_publish_report_version_transactionally_links_manifests():
    """报告版本更新与同批次导出联动在同一事务完成，冻结批次保留版本。"""
    svc = ExportManifestService()
    frozen = _fresh_manifest(svc, capture=CAPTURE_FROZEN, file_count=1, report_no="INSI-0002")
    latest = _fresh_manifest(svc, capture=CAPTURE_LATEST, file_count=1, report_no="INSI-0002")
    report_svc = InsightReportService()
    result = report_svc.publish_new_version(2, "v2.1")
    assert result["旧版本"] == "v1.2"
    assert result["联动批次数"] >= 2
    frozen_row = store.find(MANIFEST_TABLE, frozen["id"])
    latest_row = store.find(MANIFEST_TABLE, latest["id"])
    assert frozen_row["报告版本"] == "v1.2"
    assert "v2.1" in frozen_row["后续新版本"]
    assert latest_row["报告版本"] == "v2.1"
    assert latest_row["后续新版本"] == ""


def test_cross_module_cross_check_by_report_no(svc):
    """V016跨模块联动：洞察报告侧按报告编号核对文件里的结论版本。"""
    entry = _fresh_manifest(svc, capture=CAPTURE_FROZEN, file_count=1, report_no="INSI-0003")
    _complete_all_segments(svc, entry["id"])
    report_svc = InsightReportService()
    result = report_svc.cross_check_export_manifest("INSI-0003")
    assert result["报告编号"] == "INSI-0003"
    assert result["核对批次数量"] >= 1
    target = next(c for c in result["核对结果"] if c["manifest_id"] == entry["id"])
    assert target["整体通过"] is True
    assert all(f["版本一致"] and f["指纹一致"] for f in target["文件核对"])
    # 不存在的报告编号报业务错误
    with pytest.raises(ExportManifestError):
        svc.cross_check_by_report("NOPE-9999")


def test_frozen_file_keeps_old_conclusion_after_report_upgrade(svc):
    """冻结版本文件内容固化结论快照，报告升级后下载到的仍是冻结时的完整结论。"""
    entry = _fresh_manifest(svc, capture=CAPTURE_FROZEN, file_count=1, report_no="INSI-0001")
    file_row = store.filter_by_field(FILE_TABLE, "manifest_id", entry["id"])[0]
    _complete_all_segments(svc, entry["id"])
    _, before = svc.download_file(file_row["id"])
    InsightReportService().publish_new_version(1, "v9.9")
    name, after = svc.download_file(file_row["id"])
    assert before == after
    text = after.decode("utf-8")
    assert "报告版本: v1.0" in text
    assert "v9.9" not in text


def test_seed_interrupted_batch_resume_then_archive_handoff(svc):
    """种子中断批次：续写缺失片段 -> 留档核对 -> 交接的完整V016链路。"""
    plan = svc.resume_plan(2)
    missing_targets = [p for p in plan["续写计划"] if p["缺失片段"]]
    assert missing_targets and missing_targets[0]["缺失片段"] == [2]
    # 已完整的文件1在中断期间继续可下载
    _, content = svc.download_file(3)
    assert content
    svc.write_segment(4, {"片段序号": 2, "总片段数": 2})
    assert store.find(MANIFEST_TABLE, 2)["状态"] == MANIFEST_STATUS_DONE
    audit = svc.archive_check(2)
    assert audit["核对结果"] == "通过"
    handoff = svc.handoff(2, "接收方V016")
    assert handoff["核对结果"] == "通过"
    # 最终跨模块核对全部通过
    check = svc.cross_check_by_report("INSI-0002")
    assert check["全部通过"] is True

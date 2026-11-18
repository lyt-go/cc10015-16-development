"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import time
from types import TracebackType
from typing import Any

from app.seed import SEED_ROWS


class TransactionRollback(Exception):
    """事务内主动触发整体回滚时抛出。"""


class StoreTransaction:
    """内存表事务：进入时快照全部表，异常时整体恢复，保证清单与批次同生共死。"""

    def __init__(self, owner: "Store") -> None:
        self._owner = owner
        self._snapshot: dict[str, list[dict[str, Any]]] = {}
        self._version_snapshot: dict[str, int] = {}

    def __enter__(self) -> "Store":
        self._snapshot = {name: [dict(row) for row in rows] for name, rows in self._owner._tables.items()}
        self._version_snapshot = dict(self._owner._versions)
        return self._owner

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> bool:
        if exc_type is not None:
            self._owner._tables = {name: [dict(row) for row in rows]
                                   for name, rows in self._snapshot.items()}
            self._owner._versions = dict(self._version_snapshot)
        return False


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        self._created_at: dict[str, float] = {name: time.time() for name in self._tables}

    def transaction(self) -> StoreTransaction:
        """返回一个事务上下文；块内任何异常都会把所有表恢复到进入前状态。"""
        return StoreTransaction(self)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def filter_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def insert_row(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """统一的插入原语：补齐自增 id 后落表，并推进模块版本号。"""
        stored = dict(row)
        stored["id"] = int(stored.get("id") or self.next_id(module))
        self.rows(module).append(stored)
        self.bump_version(module)
        return stored

    def bump_version(self, module: str) -> int:
        self._versions[module] = self._versions.get(module, 0) + 1
        return self._versions[module]

    def version(self, module: str) -> int:
        return self._versions.get(module, 1)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

from dataclasses import dataclass, field
from typing import Iterable, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel


@dataclass
class ToolMeta:
    name: str
    description: str
    min_risk_level: str
    schema: Optional[type[BaseModel]] = None
    idempotent: bool = False


@dataclass
class ToolRegistry:
    _tools: dict[str, tuple[ToolMeta, BaseTool]] = field(default_factory=dict)

    def register(self, meta: ToolMeta, fn: BaseTool) -> None:
        self._tools[meta.name] = (meta, fn)

    def meta_for(self, name: str) -> Optional[ToolMeta]:
        """select / safety_check / recheck 消费：按工具名取元数据（P3 预检与分层校验）。"""
        entry = self._tools.get(name)
        return entry[0] if entry is not None else None

    def for_risk_level(self, level: str) -> list[BaseTool]:
        """构建期注入：只给该风险等级及以下可用的工具（tool-spec.md §1）。"""
        order = {"read": 0, "mutation": 1, "high_risk": 2}
        return [fn for meta, fn in self._tools.values() if order[meta.min_risk_level] <= order[level]]

    def tools_for_names(self, names: Iterable[str]) -> list[BaseTool]:
        """Build-time/runtime injection by explicit allowlist; registry order is preserved."""
        wanted = set(names)
        return [
            fn
            for name, (_, fn) in self._tools.items()
            if name in wanted
        ]

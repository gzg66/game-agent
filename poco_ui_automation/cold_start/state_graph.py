"""状态图层：页面转移图的沉淀、归并与持久化。

职责：
- 记录页面到页面的跳转关系
- 记录动作成功率和所属模块
- 标记高频 / 高风险 / 弹窗 / 战斗等特殊页面
- 序列化 / 反序列化为 JSON
- 生成 Mermaid 可视化图
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .semantic import PageCategory, ControlRole


# ---------------------------------------------------------------------------
# 页面状态节点
# ---------------------------------------------------------------------------

@dataclass
class PageNode:
    """状态图中的一个页面节点。"""
    signature: str
    page_id: str
    title: str
    category: str = "unknown"  # PageCategory.value
    visit_count: int = 0
    is_popup: bool = False
    is_high_risk: bool = False
    action_count: int = 0
    first_seen_step: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 状态转移边
# ---------------------------------------------------------------------------

@dataclass
class TransitionEdge:
    """状态图中的一条转移边。"""
    from_signature: str
    to_signature: str
    action_key: str
    action_label: str
    action_role: str = "unknown"  # ControlRole.value
    success_count: int = 0
    fail_count: int = 0
    is_page_changed: bool = True
    risk_level: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_attempts(self) -> int:
        return self.success_count + self.fail_count

    @property
    def success_rate(self) -> float:
        if self.total_attempts == 0:
            return 0.0
        return self.success_count / self.total_attempts


# ---------------------------------------------------------------------------
# 探索状态图
# ---------------------------------------------------------------------------

class ExplorationGraph:
    """冷启动探索的状态转移图。

    维护所有已发现的页面和它们之间的跳转关系。
    """

    def __init__(self) -> None:
        self.pages: dict[str, PageNode] = {}  # signature -> PageNode
        self.edges: list[TransitionEdge] = []
        self._page_counter: int = 0
        self._edge_lookup: dict[tuple[str, str, str], int] = {}  # (from, to, action_key) -> edges index

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    # ---- 页面操作 ----

    def add_page(
        self,
        signature: str,
        title: str,
        category: str = "unknown",
        is_popup: bool = False,
        is_high_risk: bool = False,
        action_count: int = 0,
        step: int = 0,
    ) -> tuple[PageNode, bool]:
        """添加或更新页面节点。返回 (node, is_new)。"""
        existing = self.pages.get(signature)
        if existing:
            existing.visit_count += 1
            existing.title = title
            if category != "unknown":
                existing.category = category
            existing.is_popup = existing.is_popup or is_popup
            existing.is_high_risk = existing.is_high_risk or is_high_risk
            existing.action_count = max(existing.action_count, action_count)
            return existing, False

        page_id = f"page_{self._page_counter:03d}"
        self._page_counter += 1
        node = PageNode(
            signature=signature,
            page_id=page_id,
            title=title,
            category=category,
            visit_count=1,
            is_popup=is_popup,
            is_high_risk=is_high_risk,
            action_count=action_count,
            first_seen_step=step,
        )
        self.pages[signature] = node
        return node, True

    def get_page(self, signature: str) -> PageNode | None:
        return self.pages.get(signature)

    # ---- 转移边操作 ----

    def add_transition(
        self,
        from_sig: str,
        to_sig: str,
        action_key: str,
        action_label: str,
        action_role: str = "unknown",
        success: bool = True,
        page_changed: bool = True,
        risk_level: int = 0,
    ) -> TransitionEdge:
        """添加或更新一条转移边。"""
        lookup_key = (from_sig, to_sig, action_key)
        idx = self._edge_lookup.get(lookup_key)

        if idx is not None:
            edge = self.edges[idx]
            if success:
                edge.success_count += 1
            else:
                edge.fail_count += 1
            return edge

        edge = TransitionEdge(
            from_signature=from_sig,
            to_signature=to_sig,
            action_key=action_key,
            action_label=action_label,
            action_role=action_role,
            success_count=1 if success else 0,
            fail_count=0 if success else 1,
            is_page_changed=page_changed,
            risk_level=risk_level,
        )
        self._edge_lookup[lookup_key] = len(self.edges)
        self.edges.append(edge)
        return edge

    # ---- 查询 ----

    def get_transitions_from(self, signature: str) -> list[TransitionEdge]:
        """获取从指定页面出发的所有转移边。"""
        return [e for e in self.edges if e.from_signature == signature]

    def get_transitions_to(self, signature: str) -> list[TransitionEdge]:
        """获取跳转到指定页面的所有转移边。"""
        return [e for e in self.edges if e.to_signature == signature]

    def high_frequency_pages(self, min_visits: int = 3) -> list[PageNode]:
        """获取高频出现的页面。"""
        return [p for p in self.pages.values() if p.visit_count >= min_visits]

    def popup_pages(self) -> list[PageNode]:
        """获取弹窗类页面。"""
        return [p for p in self.pages.values() if p.is_popup]

    def high_risk_pages(self) -> list[PageNode]:
        """获取高风险页面。"""
        return [p for p in self.pages.values() if p.is_high_risk]

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "pages": {sig: asdict(node) for sig, node in self.pages.items()},
            "edges": [asdict(edge) for edge in self.edges],
            "page_count": self.page_count,
            "edge_count": self.edge_count,
        }

    def save(self, path: Path) -> None:
        """保存到 JSON 文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> ExplorationGraph:
        """从 JSON 文件加载。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        graph = cls()
        for sig, page_data in data.get("pages", {}).items():
            node = PageNode(**page_data)
            graph.pages[sig] = node
            graph._page_counter = max(graph._page_counter,
                                      int(node.page_id.replace("page_", "")) + 1)
        for edge_data in data.get("edges", []):
            edge = TransitionEdge(**edge_data)
            lookup_key = (edge.from_signature, edge.to_signature, edge.action_key)
            graph._edge_lookup[lookup_key] = len(graph.edges)
            graph.edges.append(edge)
        return graph

    # ---- Mermaid 可视化 ----

    def to_mermaid(self) -> str:
        """生成 Mermaid 流程图。"""
        lines = ["graph TD"]

        for sig, node in self.pages.items():
            node_id = _safe_mermaid_id(sig)
            label = node.title.replace('"', "'")
            category = node.category
            if node.is_popup:
                label = f"📌 {label}"
            if node.is_high_risk:
                label = f"⚠ {label}"
            lines.append(f'    {node_id}["{label}<br/>({category})"]')

        for edge in self.edges:
            if not edge.is_page_changed:
                continue
            from_id = _safe_mermaid_id(edge.from_signature)
            to_id = _safe_mermaid_id(edge.to_signature)
            label = edge.action_label.replace('"', "'")[:30]
            rate = f"{edge.success_rate:.0%}" if edge.total_attempts > 0 else ""
            lines.append(f'    {from_id} -->|"{label} {rate}"| {to_id}')

        return "\n".join(lines) + "\n"


def _safe_mermaid_id(raw: str) -> str:
    """生成 Mermaid 安全的节点 ID。"""
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in raw)

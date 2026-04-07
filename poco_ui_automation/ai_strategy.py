from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Iterable

from .models import ActionCandidate, PageSnapshot, StateEdge, StateNode, UiNode


def _normalize_label(node: UiNode) -> str:
    return (node.text or node.name or node.node_id or "").strip().lower()


@dataclass(slots=True)
class RuleScenario:
    name: str
    preferred_keywords: list[str] = field(default_factory=list)
    blocked_keywords: list[str] = field(default_factory=list)
    expected_pages: list[str] = field(default_factory=list)


class StateGraphMemory:
    def __init__(self) -> None:
        self.nodes: dict[str, StateNode] = {}
        self.edges: dict[tuple[str, str, str, str], StateEdge] = {}

    def remember_node(self, snapshot: PageSnapshot) -> None:
        key_nodes = [node.label() for node in snapshot.nodes[:12] if node.label()]
        ui_hash = hashlib.sha1(
            "|".join(sorted(key_nodes + snapshot.root_names + snapshot.key_texts)).encode("utf-8")
        ).hexdigest()
        self.nodes[snapshot.signature] = StateNode(
            signature=snapshot.signature,
            page_name=snapshot.page_name,
            key_nodes=key_nodes,
            ui_hash=ui_hash,
        )

    def remember_edge(
        self,
        from_signature: str,
        to_signature: str,
        action_type: str,
        selector_key: str,
        duration_ms: int,
        success: bool,
        performance: dict[str, float] | None = None,
    ) -> None:
        edge_key = (from_signature, to_signature, action_type, selector_key)
        edge = self.edges.get(edge_key)
        if edge:
            edge.count += 1
            edge.duration_ms = min(edge.duration_ms, duration_ms)
            edge.success = edge.success or success
            if performance:
                edge.performance.update(performance)
            return
        self.edges[edge_key] = StateEdge(
            from_signature=from_signature,
            to_signature=to_signature,
            action_type=action_type,
            selector_key=selector_key,
            duration_ms=duration_ms,
            success=success,
            performance=performance or {},
        )

    def seen_transition(self, from_signature: str, selector_key: str) -> bool:
        return any(
            edge.from_signature == from_signature and edge.selector_key == selector_key
            for edge in self.edges.values()
        )


class HybridPlanner:
    """规则优先，启发式打分兜底。"""

    def __init__(self, dangerous_keywords: Iterable[str] | None = None) -> None:
        self.dangerous_keywords = {item.lower() for item in (dangerous_keywords or [])}

    def plan(
        self,
        snapshot: PageSnapshot,
        memory: StateGraphMemory,
        goal: str,
        scenario: RuleScenario | None = None,
    ) -> list[ActionCandidate]:
        candidates: list[ActionCandidate] = []
        for node in snapshot.nodes:
            if not node.visible or not node.enabled or not node.clickable:
                continue
            label = _normalize_label(node)
            if not label:
                continue
            score = self._base_score(label, node, memory, snapshot.signature, goal)
            reason_bits = []
            if scenario:
                score += self._score_scenario(label, scenario, reason_bits)
            if any(word in label for word in self.dangerous_keywords):
                score -= 100.0
                reason_bits.append("命中危险动作关键字")
            if memory.seen_transition(snapshot.signature, label):
                score -= 8.0
                reason_bits.append("该路径已探索过")
            if score <= 0:
                continue
            candidates.append(
                ActionCandidate(
                    action_type="click",
                    selector_key=label,
                    selector_query=node.name or node.text or label,
                    reason="; ".join(reason_bits) or "启发式高分节点",
                    score=score,
                    confidence=min(score / 20.0, 0.99),
                    expected_page=scenario.expected_pages[0] if scenario and scenario.expected_pages else None,
                    metadata={"text": node.text, "name": node.name},
                )
            )
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    def _base_score(
        self,
        label: str,
        node: UiNode,
        memory: StateGraphMemory,
        signature: str,
        goal: str,
    ) -> float:
        score = 1.0
        if any(
            keyword in label
            for keyword in (
                "开始",
                "进入",
                "确认",
                "下一步",
                "领取",
                "设置",
                "商城",
                "start",
                "play",
                "next",
                "ok",
                "confirm",
                "setting",
                "shop",
                "menu",
            )
        ):
            score += 8.0
        if any(keyword in label for keyword in ("关闭", "跳过", "返回", "取消", "back", "close", "cancel")):
            score += 3.0
        if label.startswith("btn_") or "button" in label:
            score += 4.0
        if goal and goal.lower() in label:
            score += 10.0
        if node.text and any(ch.isdigit() for ch in node.text):
            score += 2.0
        if not memory.seen_transition(signature, label):
            score += 6.0
        return score

    def _score_scenario(self, label: str, scenario: RuleScenario, reason_bits: list[str]) -> float:
        delta = 0.0
        if any(keyword.lower() in label for keyword in scenario.preferred_keywords):
            delta += 12.0
            reason_bits.append("命中规则优先关键字")
        if any(keyword.lower() in label for keyword in scenario.blocked_keywords):
            delta -= 30.0
            reason_bits.append("命中规则阻断关键字")
        return delta

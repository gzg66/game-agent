from __future__ import annotations

from typing import TYPE_CHECKING

from .models import CandidateAction, RiskLevel, SemanticNode, WidgetRole

if TYPE_CHECKING:
    from .ai_strategy import RuleScenario, StateGraphMemory


_TIER1_ROLES: set[str] = {
    WidgetRole.CLOSE.value,
    WidgetRole.CONFIRM.value,
    WidgetRole.SKIP.value,
    WidgetRole.REWARD_CLAIM.value,
    WidgetRole.PRIMARY_ENTRY.value,
}
_TIER2_ROLES: set[str] = {
    WidgetRole.BACK.value,
    WidgetRole.SECONDARY_ENTRY.value,
    WidgetRole.BATTLE_START.value,
    WidgetRole.BATTLE_AUTO.value,
    WidgetRole.BATTLE_SETTLEMENT.value,
    WidgetRole.CANCEL.value,
}
_TIER3_ROLES: set[str] = {
    WidgetRole.SHOP_ENTRY.value,
    WidgetRole.PAY_ENTRY.value,
    WidgetRole.DANGEROUS_ACTION.value,
    WidgetRole.UNKNOWN_ACTION.value,
}

_TIER1_BASE = 80.0
_TIER2_BASE = 50.0
_TIER3_BASE = 20.0

_INTENT_MAP: dict[str, str] = {
    WidgetRole.CLOSE.value: "关闭弹窗",
    WidgetRole.BACK.value: "返回上一页",
    WidgetRole.CONFIRM.value: "确认操作",
    WidgetRole.CANCEL.value: "取消操作",
    WidgetRole.SKIP.value: "跳过当前步骤",
    WidgetRole.REWARD_CLAIM.value: "领取奖励",
    WidgetRole.PRIMARY_ENTRY.value: "进入功能",
    WidgetRole.SECONDARY_ENTRY.value: "进入次级功能",
    WidgetRole.BATTLE_START.value: "开始战斗",
    WidgetRole.BATTLE_AUTO.value: "开启自动战斗",
    WidgetRole.BATTLE_SETTLEMENT.value: "结算战斗结果",
    WidgetRole.SHOP_ENTRY.value: "进入商城",
    WidgetRole.PAY_ENTRY.value: "进入付费入口",
    WidgetRole.DANGEROUS_ACTION.value: "危险操作",
    WidgetRole.UNKNOWN_ACTION.value: "执行未知操作",
}


class CandidateGenerator:
    """三级优先级候选动作生成器。"""

    def __init__(
        self,
        dangerous_keywords: set[str] | None = None,
        max_candidates_per_page: int = 8,
    ) -> None:
        self._dangerous = {kw.lower() for kw in (dangerous_keywords or set())}
        self._max = max_candidates_per_page

    def generate(
        self,
        page_signature: str,
        semantic_nodes: list[SemanticNode],
        memory: "StateGraphMemory",
        goal: str = "",
        scenario: "RuleScenario | None" = None,
    ) -> list[CandidateAction]:
        candidates: list[CandidateAction] = []
        for idx, node in enumerate(semantic_nodes):
            if not node.visible or not node.enabled or not node.clickable:
                continue
            score = self._compute_priority(
                node, memory, page_signature, goal, scenario
            )
            if score <= 0:
                continue
            intent = _INTENT_MAP.get(node.semantic_role, "执行操作")
            candidates.append(
                CandidateAction(
                    action_id=f"ca_{idx}_{node.node_id}",
                    page_signature=page_signature,
                    selector_query=node.raw_name or node.raw_text or node.normalized_label,
                    target_node_id=node.node_id,
                    semantic_intent=intent,
                    semantic_role=node.semantic_role,
                    reason=self._build_reason(node, memory, page_signature),
                    source="ui_tree",
                    risk_level=node.risk_level,
                    priority_score=score,
                    expected_result="",
                    metadata={
                        "raw_name": node.raw_name,
                        "raw_text": node.raw_text,
                    },
                )
            )
        candidates.sort(key=lambda c: c.priority_score, reverse=True)
        return candidates[: self._max]

    def _compute_priority(
        self,
        node: SemanticNode,
        memory: "StateGraphMemory",
        page_signature: str,
        goal: str,
        scenario: "RuleScenario | None",
    ) -> float:
        role = node.semantic_role
        if role in _TIER1_ROLES:
            score = _TIER1_BASE
        elif role in _TIER2_ROLES:
            score = _TIER2_BASE
        else:
            score = _TIER3_BASE

        if not memory.seen_transition(page_signature, node.normalized_label):
            score += 30.0

        if goal and goal.lower() in node.normalized_label:
            score += 20.0

        if scenario:
            for kw in scenario.preferred_keywords:
                if kw.lower() in node.normalized_label:
                    score += 15.0
                    break
            for kw in scenario.blocked_keywords:
                if kw.lower() in node.normalized_label:
                    score -= 40.0
                    break

        if node.risk_level == RiskLevel.HIGH.value:
            score -= 100.0

        if memory.seen_transition(page_signature, node.normalized_label):
            score -= 20.0

        return score

    @staticmethod
    def _build_reason(
        node: SemanticNode,
        memory: "StateGraphMemory",
        page_signature: str,
    ) -> str:
        parts: list[str] = []
        parts.append(f"角色={node.semantic_role}")
        if not memory.seen_transition(page_signature, node.normalized_label):
            parts.append("未探索路径")
        else:
            parts.append("已探索路径")
        if node.risk_level != RiskLevel.NONE.value:
            parts.append(f"风险={node.risk_level}")
        return "; ".join(parts)

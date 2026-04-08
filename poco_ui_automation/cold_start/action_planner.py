"""动作规划层：候选动作生成与冷启动优先级排序。

职责：
- 从页面语义信息生成候选动作
- 按冷启动策略排序（低风险、高价值优先）
- 控制每页探索动作数量（3-8 个）
- 对已探索动作降权
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import GameConfig
from .observation import ObservedNode
from .semantic import ControlRole, NodeSemanticInfo, PageSemanticInfo


# ---------------------------------------------------------------------------
# 候选动作
# ---------------------------------------------------------------------------

@dataclass
class CandidateAction:
    """一个冷启动候选动作。"""
    node: ObservedNode
    role: ControlRole
    priority: float
    reason: str
    risk_level: int = 0

    @property
    def action_key(self) -> str:
        return self.node.action_key

    @property
    def label(self) -> str:
        return self.node.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "label": self.label,
            "name": self.node.name,
            "text": self.node.text,
            "path": self.node.path,
            "type": self.node.node_type,
            "pos": self.node.pos,
            "size": self.node.size,
            "role": self.role.value,
            "priority": round(self.priority, 2),
            "risk_level": self.risk_level,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 动作规划器
# ---------------------------------------------------------------------------

class ColdStartActionPlanner:
    """冷启动阶段的动作规划器。

    与通用 HybridPlanner 的区别：
    - 更保守的风险控制
    - 基于设计文档的明确优先级梯队
    - 限制每页动作数
    - 支持已探索动作降权
    """

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self._explored_actions: set[str] = set()  # page_sig::action_key

    def plan(
        self,
        page_semantic: PageSemanticInfo,
        explored_on_page: set[str] | None = None,
    ) -> list[CandidateAction]:
        """生成排序后的候选动作列表。

        Args:
            page_semantic: 页面语义分析结果
            explored_on_page: 该页面上已探索过的 action_key 集合

        Returns:
            按优先级排序的候选动作列表（已截取到 max_actions_per_page）
        """
        explored = explored_on_page or set()
        candidates: list[CandidateAction] = []

        for sem in page_semantic.node_semantics:
            node = sem.node

            # 过滤不可见节点
            if not node.visible:
                continue

            # 过滤无效位置
            if not _is_valid_pos(node):
                continue

            # 计算优先级
            priority = sem.priority_score
            reason_parts: list[str] = []

            if sem.role != ControlRole.UNKNOWN:
                reason_parts.append(f"角色={sem.role.value}")

            # 已探索过的动作降权
            scope_key = f"{page_semantic.observation.signature}::{node.action_key}"
            if node.action_key in explored or scope_key in self._explored_actions:
                priority -= 20.0
                reason_parts.append("已探索过")

            # 弹窗页面中的关闭按钮加权
            if page_semantic.has_popup and sem.role == ControlRole.CLOSE:
                priority += 15.0
                reason_parts.append("弹窗关闭按钮加权")

            # 安全关键字匹配加分
            node_text = f"{node.name} {node.text}".lower()
            safe_match = [kw for kw in self.config.safe_priority_keywords if kw.lower() in node_text]
            if safe_match:
                priority += 5.0
                reason_parts.append(f"安全关键字: {','.join(safe_match[:3])}")

            # 跳过低优先级的危险动作
            if sem.risk_level >= 2:
                priority = min(priority, -10.0)
                reason_parts.append("⚠ 危险动作")

            candidates.append(CandidateAction(
                node=node,
                role=sem.role,
                priority=priority,
                reason="; ".join(reason_parts) if reason_parts else "启发式",
                risk_level=sem.risk_level,
            ))

        # 按优先级排序
        candidates.sort(key=lambda c: c.priority, reverse=True)

        # 限制每页动作数
        max_actions = self.config.max_actions_per_page
        return candidates[:max_actions]

    def mark_explored(self, page_signature: str, action_key: str) -> None:
        """标记一个动作已被探索。"""
        self._explored_actions.add(f"{page_signature}::{action_key}")

    def is_explored(self, page_signature: str, action_key: str) -> bool:
        """检查某动作是否已探索。"""
        return f"{page_signature}::{action_key}" in self._explored_actions

    @property
    def total_explored(self) -> int:
        return len(self._explored_actions)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _is_valid_pos(node: ObservedNode) -> bool:
    """检查节点位置是否有效。"""
    pos = node.pos
    if not isinstance(pos, list) or len(pos) != 2:
        return False
    x, y = pos
    return (isinstance(x, (int, float)) and isinstance(y, (int, float))
            and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0)

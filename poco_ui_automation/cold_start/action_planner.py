"""动作规划层：候选动作生成与冷启动优先级排序。

职责：
- 从页面语义信息生成候选动作
- 引入结构化的动作梯队（Tier），避免通过单一浮点数硬算
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
    tier: int = 2  # 【核心修改 1】：新增动作梯队属性，数值越大越优先执行
    confidence: float = 0.0
    semantic_source: str = "rule"

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
            "tier": self.tier,
            "confidence": round(self.confidence, 3),
            "semantic_source": self.semantic_source,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 动作规划器
# ---------------------------------------------------------------------------

class ColdStartActionPlanner:
    """冷启动阶段的动作规划器。"""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self._explored_actions: set[str] = set()  # page_sig::action_key

    def plan(
        self,
        page_semantic: PageSemanticInfo,
        explored_on_page: set[str] | None = None,
    ) -> list[CandidateAction]:
        explored = explored_on_page or set()
        candidates: list[CandidateAction] = []

        for sem in page_semantic.node_semantics:
            node = sem.node

            # 过滤不可见节点和无效位置
            if not sem.is_actionable:
                continue
            if not node.visible or not _is_valid_pos(node):
                continue
            if (
                getattr(self.config, "vision_mode", "rule_first") == "vision_first"
                and not getattr(self.config, "vision_allow_low_confidence", False)
                and sem.semantic_source != "rule_degraded"
                and sem.confidence < getattr(self.config, "vision_min_confidence", 0.55)
            ):
                continue

            priority = sem.priority_score + sem.confidence * 20.0
            reason_parts: list[str] = []
            
            # 【核心修改 2】：默认所有的动作都属于正向探索梯队 (Tier 2)
            tier = 2  

            if sem.role != ControlRole.UNKNOWN:
                reason_parts.append(f"角色={sem.role.value}")
            reason_parts.append(f"置信度={sem.confidence:.2f}")
            reason_parts.append(f"来源={sem.semantic_source}")

            # 已探索过的动作降权（同梯队内降低优先级）
            scope_key = f"{page_semantic.observation.signature}::{node.action_key}"
            if node.action_key in explored or scope_key in self._explored_actions:
                priority -= 20.0
                reason_parts.append("已探索过")

            # 【核心修改 3】：结构化分配梯队
            if page_semantic.has_popup and sem.role == ControlRole.CLOSE:
                tier = 3  # Tier 3: 弹窗中断梯队（最紧急）
                reason_parts.append("Tier 3: 弹窗关闭")
            elif not page_semantic.has_popup and sem.role in {ControlRole.BACK, ControlRole.CLOSE}:
                if page_semantic.category.value == "lobby":
                    tier = 0  # Tier 0: 大厅的返回/退出按钮，属于禁区
                    reason_parts.append("Tier 0: 大厅防退绝对禁区")
                else:
                    tier = 1  # Tier 1: 常规页面的兜底回退梯队
                    reason_parts.append("Tier 1: 兜底回退结构后置")

            # 安全关键字匹配加分（仅影响同梯队内部排名）
            node_text = f"{node.name} {node.text}".lower()
            safe_match = [kw for kw in self.config.safe_priority_keywords if kw.lower() in node_text]
            if safe_match:
                priority += 5.0
                reason_parts.append(f"安全关键字: {','.join(safe_match[:3])}")

            # 跳过低优先级的危险动作
            if sem.risk_level >= 2:
                priority = min(priority, -10.0)
                reason_parts.append("⚠ 危险动作")

            # 如果是大厅的返回键 (Tier 0)，直接过滤掉，连候补名单都不进
            if tier == 0:
                continue

            candidates.append(CandidateAction(
                node=node,
                role=sem.role,
                priority=priority,
                reason="; ".join(reason_parts) if reason_parts else "启发式",
                risk_level=sem.risk_level,
                tier=tier, # 录入梯队信息
                confidence=sem.confidence,
                semantic_source=sem.semantic_source,
            ))

        # 【核心修改 4】：使用组合键排序 (Tuple Sorting)
        # 先按 tier 降序排，tier 相同的再按 priority 降序排
        # 这样能从物理结构上保证 Tier 2 的关卡按钮永远在 Tier 1 的返回按钮前面！
        candidates.sort(key=lambda c: (c.tier, c.priority), reverse=True)

        # 限制每页动作数
        max_actions = self.config.max_actions_per_page
        return candidates[:max_actions]

    def mark_explored(self, page_signature: str, action_key: str) -> None:
        self._explored_actions.add(f"{page_signature}::{action_key}")

    def is_explored(self, page_signature: str, action_key: str) -> bool:
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
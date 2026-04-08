"""语义层：页面类别分类、控件语义角色识别、风险标注。

职责：
- 给页面打类别标签（login / dialog / lobby / guide / reward / shop / battle 等）
- 给控件打语义角色（close / back / confirm / skip / reward_claim / primary_entry 等）
- 标记高风险节点
- 输出 PageSemanticInfo 和 NodeSemanticInfo
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import GameConfig
from .observation import ObservedNode, PageObservation


# ---------------------------------------------------------------------------
# 页面类型枚举
# ---------------------------------------------------------------------------

class PageCategory(str, Enum):
    LOGIN = "login"
    DIALOG = "dialog"
    LOBBY = "lobby"
    GUIDE = "guide"
    REWARD = "reward"
    BATTLE_PREPARE = "battle_prepare"
    BATTLE_RUNNING = "battle_running"
    BATTLE_RESULT = "battle_result"
    SHOP = "shop"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 控件语义角色枚举
# ---------------------------------------------------------------------------

class ControlRole(str, Enum):
    CLOSE = "close"
    BACK = "back"
    CONFIRM = "confirm"
    SKIP = "skip"
    REWARD_CLAIM = "reward_claim"
    PRIMARY_ENTRY = "primary_entry"
    BATTLE_START = "battle_start"
    DANGEROUS_ACTION = "dangerous_action"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 语义信息数据类
# ---------------------------------------------------------------------------

@dataclass
class NodeSemanticInfo:
    """单个控件的语义信息。"""
    node: ObservedNode
    role: ControlRole = ControlRole.UNKNOWN
    risk_level: int = 0  # 0=安全 1=低风险 2=高风险
    priority_score: float = 0.0
    role_reason: str = ""


@dataclass
class PageSemanticInfo:
    """一个页面的语义信息。"""
    observation: PageObservation
    category: PageCategory = PageCategory.UNKNOWN
    category_confidence: float = 0.0
    category_reason: str = ""
    has_popup: bool = False
    has_high_risk: bool = False
    node_semantics: list[NodeSemanticInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 语义分析器
# ---------------------------------------------------------------------------

class SemanticAnalyzer:
    """基于关键字规则的语义分析器。

    使用 GameConfig 中的 page_type_hints 和 control_role_hints 进行匹配。
    后续可替换为 LLM 驱动的分析器。
    """

    def __init__(self, config: GameConfig) -> None:
        self.config = config

    def analyze(self, observation: PageObservation) -> PageSemanticInfo:
        """对一次页面观测执行完整语义分析。"""
        # 1. 页面分类
        category, confidence, reason = self._classify_page(observation)

        # 2. 控件语义识别
        node_semantics: list[NodeSemanticInfo] = []
        has_high_risk = False
        has_popup = category == PageCategory.DIALOG

        for node in observation.clickable_nodes:
            sem = self._classify_node(node)
            node_semantics.append(sem)
            if sem.risk_level >= 2:
                has_high_risk = True

        # 检查是否存在弹窗特征
        if not has_popup:
            has_popup = self._detect_popup(observation)

        return PageSemanticInfo(
            observation=observation,
            category=category,
            category_confidence=confidence,
            category_reason=reason,
            has_popup=has_popup,
            has_high_risk=has_high_risk,
            node_semantics=node_semantics,
        )

    # ---- 页面分类 ----

    def _classify_page(self, obs: PageObservation) -> tuple[PageCategory, float, str]:
        """对页面进行类型分类。"""
        # 收集页面中所有文本信息
        all_text_parts: list[str] = []
        for node in obs.all_nodes:
            if node.text:
                all_text_parts.append(node.text.lower())
            if node.name:
                all_text_parts.append(node.name.lower())
        all_text = " ".join(all_text_parts)

        best_category = PageCategory.UNKNOWN
        best_score = 0
        best_reason = ""

        for cat_name, keywords in self.config.page_type_hints.items():
            score = 0
            matched: list[str] = []
            for kw in keywords:
                if kw.lower() in all_text:
                    score += 1
                    matched.append(kw)
            if score > best_score:
                best_score = score
                try:
                    best_category = PageCategory(cat_name)
                except ValueError:
                    best_category = PageCategory.UNKNOWN
                best_reason = f"匹配关键字: {', '.join(matched)}"

        # 简单置信度计算
        confidence = min(best_score / 3.0, 1.0) if best_score > 0 else 0.0
        return best_category, confidence, best_reason

    # ---- 控件分类 ----

    def _classify_node(self, node: ObservedNode) -> NodeSemanticInfo:
        """对单个可交互控件进行语义角色分类。"""
        node_text = f"{node.name} {node.text} {node.path}".lower()

        best_role = ControlRole.UNKNOWN
        best_score = 0
        best_reason = ""

        for role_name, keywords in self.config.control_role_hints.items():
            score = 0
            matched: list[str] = []
            for kw in keywords:
                if kw.lower() in node_text:
                    score += 1
                    matched.append(kw)
            if score > best_score:
                best_score = score
                try:
                    best_role = ControlRole(role_name)
                except ValueError:
                    best_role = ControlRole.UNKNOWN
                best_reason = f"匹配: {', '.join(matched)}"

        # 风险评估
        risk_level = 0
        if best_role == ControlRole.DANGEROUS_ACTION:
            risk_level = 2
        elif any(kw.lower() in node_text for kw in self.config.dangerous_keywords):
            risk_level = 2
            best_role = ControlRole.DANGEROUS_ACTION
            best_reason = "命中危险关键字"

        # 优先级评分
        priority_score = self._compute_priority(best_role, node, risk_level)

        return NodeSemanticInfo(
            node=node,
            role=best_role,
            risk_level=risk_level,
            priority_score=priority_score,
            role_reason=best_reason,
        )

    def _compute_priority(self, role: ControlRole, node: ObservedNode, risk_level: int) -> float:
        """计算控件探索优先级。

        冷启动阶段优先级：
        第一优先级 (高分): close > confirm > next/start/enter > claim
        第二优先级 (中分): back > skip > 功能入口 > 战斗入口
        第三优先级 (低分): 不明确用途
        降权: 危险动作
        """
        _ROLE_PRIORITIES: dict[ControlRole, float] = {
            ControlRole.CLOSE: 90.0,       # 关闭弹窗最优先
            ControlRole.CONFIRM: 85.0,     # 确认
            ControlRole.PRIMARY_ENTRY: 80.0,  # 开始/进入
            ControlRole.REWARD_CLAIM: 75.0,   # 领取
            ControlRole.BACK: 60.0,        # 返回
            ControlRole.SKIP: 55.0,        # 跳过
            ControlRole.BATTLE_START: 50.0,   # 战斗入口
            ControlRole.UNKNOWN: 30.0,     # 不明确用途
            ControlRole.DANGEROUS_ACTION: -50.0,  # 危险动作
        }

        score = _ROLE_PRIORITIES.get(role, 30.0)

        # 有文本的控件优先（可读性强）
        if node.text:
            score += 5.0

        # 可见且位置有效的控件优先
        if node.pos and isinstance(node.pos, list) and len(node.pos) == 2:
            x, y = node.pos
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                    score += 3.0

        # 深度浅的控件优先
        if node.depth <= 3:
            score += 5.0
        elif node.depth <= 6:
            score += 2.0

        if risk_level >= 2:
            score -= 100.0

        return score

    # ---- 弹窗检测 ----

    def _detect_popup(self, obs: PageObservation) -> bool:
        """检测是否存在弹窗。"""
        popup_hints = ["dialog", "popup", "modal", "notice", "弹窗", "提示", "公告"]
        for node in obs.all_nodes[:20]:  # 只看前 20 个节点
            combined = f"{node.name} {node.text}".lower()
            if any(hint in combined for hint in popup_hints):
                return True
        # 如果有 close 按钮在浅层级，可能是弹窗
        for node in obs.clickable_nodes:
            if node.depth <= 3:
                combined = f"{node.name} {node.text}".lower()
                if any(kw in combined for kw in ["close", "关闭", "x"]):
                    return True
        return False

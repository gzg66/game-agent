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
import re

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
    confidence: float = 0.0
    semantic_source: str = "rule"
    is_actionable: bool = True
    actionability_reason: str = ""
    blocked_reason: str = ""
    unlock_hint_text: str = ""
    unlock_condition: str = ""


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
    semantic_source: str = "rule"
    cache_hit: bool = False
    llm_candidate_count: int = 0
    llm_enriched_node_count: int = 0
    llm_pending: bool = False
    actionable_candidate_count: int = 0
    blocked_action_count: int = 0
    degraded_mode: bool = False


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

        for node in observation.actionable_candidates:
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
            actionable_candidate_count=len(observation.actionable_candidates),
            blocked_action_count=sum(
                1
                for sem in node_semantics
                if sem.blocked_reason or sem.unlock_hint_text
            ),
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
        best_max_kw_len = 0
        best_reason = ""

        for cat_name, keywords in self.config.page_type_hints.items():
            score = 0
            matched: list[str] = []
            max_kw_len = 0
            for kw in keywords:
                if _page_type_keyword_matches(kw, all_text):
                    score += 1
                    matched.append(kw)
                    max_kw_len = max(max_kw_len, len(kw))
            # 平局时优先「更长关键词命中」的类别，避免仅因 dict 顺序把带调试条「账号:」的局内页判成 login
            better = score > best_score or (
                score == best_score and score > 0 and max_kw_len > best_max_kw_len
            )
            if better:
                best_score = score
                best_max_kw_len = max_kw_len
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
        """对单个候选控件进行语义角色分类。"""
        node_text = f"{node.name} {node.text}".lower()

        best_role = ControlRole.UNKNOWN
        best_score = 0
        best_reason = ""

        for role_name, keywords in self.config.control_role_hints.items():
            score = 0
            matched: list[str] = []
            for kw in keywords:
                if _keyword_matches_node(kw, node_text):
                    score += 1
                    matched.append(kw)
            if score > best_score:
                best_score = score
                try:
                    best_role = ControlRole(role_name)
                except ValueError:
                    best_role = ControlRole.UNKNOWN
                best_reason = f"匹配: {', '.join(matched)}"

        confidence = self._compute_rule_confidence(best_score, node)
        best_role, best_reason, confidence = self._apply_structural_heuristics(
            node,
            best_role,
            best_reason,
            confidence,
            best_score,
        )

        # 风险评估
        risk_level = 0
        if best_role == ControlRole.DANGEROUS_ACTION:
            risk_level = 2
        elif any(_keyword_matches_node(kw, node_text) for kw in self.config.dangerous_keywords):
            risk_level = 2
            best_role = ControlRole.DANGEROUS_ACTION
            best_reason = "命中危险关键字"
            confidence = max(confidence, 0.95)

        # 优先级评分
        priority_score = self._compute_priority(best_role, node, risk_level, confidence)

        return NodeSemanticInfo(
            node=node,
            role=best_role,
            risk_level=risk_level,
            priority_score=priority_score,
            role_reason=best_reason,
            confidence=confidence,
            semantic_source="rule",
            is_actionable=True,
            actionability_reason=node.candidate_reason or "rule_candidate",
        )

    def _apply_structural_heuristics(
        self,
        node: ObservedNode,
        best_role: ControlRole,
        best_reason: str,
        confidence: float,
        best_score: int,
    ) -> tuple[ControlRole, str, float]:
        if best_score > 0:
            return best_role, best_reason, confidence

        lowered_name = (node.name or "").lower()
        lowered_text = (node.text or "").lower()
        lowered_type = (node.node_type or "").lower()
        combined = f"{lowered_name} {lowered_text}"

        if lowered_name.startswith("btn") and any(token in combined for token in ("login", "登录", "enter", "start", "submit")):
            return ControlRole.PRIMARY_ENTRY, "结构启发: btn + entry token", max(confidence, 0.7)
        if lowered_name.startswith("btn") and any(token in combined for token in ("close", "关闭", "back", "返回")):
            # 名称里含 back/close 但文案是「退出」等时，不得当成返回（常见于主界面无返回、仅有退出）
            exit_tokens = ("退出", "登出", "quit", "logout")
            if any(t in combined for t in exit_tokens):
                return ControlRole.DANGEROUS_ACTION, "结构启发: 疑似退出/登出，非返回", max(confidence, 0.85)
            role = ControlRole.CLOSE if any(token in combined for token in ("close", "关闭")) else ControlRole.BACK
            return role, "结构启发: btn + return token", max(confidence, 0.7)
        if lowered_name.startswith("input") or lowered_type == "editbox":
            return best_role, "结构启发: 输入控件", max(confidence, 0.35)

        return best_role, best_reason, confidence

    def _compute_rule_confidence(self, best_score: int, node: ObservedNode) -> float:
        confidence = 0.2
        if best_score > 0:
            confidence = min(0.35 + best_score * 0.2, 0.9)
        if node.clickable:
            confidence += 0.05
        if node.interactive:
            confidence += 0.05
        if node.candidate_score >= 4.0:
            confidence += 0.05
        return min(confidence, 0.95)

    def _compute_priority(
        self,
        role: ControlRole,
        node: ObservedNode,
        risk_level: int,
        confidence: float,
    ) -> float:
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

        score += confidence * 10.0
        score += min(node.candidate_score, 6.0) * 2.0

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
        for node in obs.actionable_candidates:
            if node.depth <= 3:
                combined = f"{node.name} {node.text}".lower()
                if any(kw in combined for kw in ["close", "关闭", "x"]):
                    return True
        return False


def _page_type_keyword_matches(keyword: str, haystack: str) -> bool:
    """页面类型提示词是否命中。短词易与调试文案冲突时单独处理。"""
    kw = keyword.strip()
    if not kw:
        return False
    low_kw = kw.lower()
    low_hay = haystack.lower()
    # 顶栏「账号: xxx」「账号：xxx」勿当作登录页特征
    if low_kw == "账号":
        return bool(re.search(r"账号(?![:：])", haystack))
    if low_kw == "account":
        return bool(re.search(r"(?<![a-z0-9_])account(?![a-z0-9_:：])", low_hay))
    return low_kw in low_hay


def _keyword_matches_node(keyword: str, node_text: str) -> bool:
    lowered_keyword = keyword.lower().strip()
    if not lowered_keyword:
        return False
    if len(lowered_keyword) == 1 and lowered_keyword.isascii() and lowered_keyword.isalpha():
        pattern = rf"(?<![a-z0-9_]){re.escape(lowered_keyword)}(?![a-z0-9_])"
        return bool(re.search(pattern, node_text))
    return lowered_keyword in node_text

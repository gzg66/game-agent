from __future__ import annotations

from typing import TYPE_CHECKING

from .models import (
    PageObservation,
    PageType,
    RiskLevel,
    SemanticNode,
    SemanticPageState,
    UiNode,
    WidgetRole,
    utc_now,
)

if TYPE_CHECKING:
    from .integration import ProjectProfile


def _label(node: UiNode) -> str:
    return (node.text or node.name or "").strip().lower()


# ---------------------------------------------------------------------------
# 页面类别分类器
# ---------------------------------------------------------------------------

_PAGE_RULES: list[tuple[str, list[str], float]] = [
    (PageType.LOGIN.value, ["登录", "开始游戏", "游客登录", "账号登录", "login", "sign in"], 0.80),
    (PageType.LOBBY.value, ["大厅", "冒险", "背包", "任务", "邮件", "lobby"], 0.85),
    (PageType.GUIDE.value, ["引导", "教程", "新手", "guide", "tutorial"], 0.70),
    (PageType.REWARD.value, ["奖励", "领取", "恭喜", "reward", "claim"], 0.75),
    (PageType.SHOP.value, ["商城", "商店", "shop", "store"], 0.70),
    (PageType.BATTLE_RUNNING.value, ["自动", "暂停", "auto", "pause"], 0.65),
    (PageType.BATTLE_PREPARE.value, ["出战", "挑战", "ready", "prepare"], 0.65),
    (PageType.BATTLE_RESULT.value, ["结算", "胜利", "失败", "victory", "defeat", "result"], 0.70),
]


class PageClassifier:
    """基于关键词规则的页面类别分类器。"""

    def classify(
        self,
        observation: PageObservation,
        profile: "ProjectProfile | None" = None,
    ) -> tuple[str, float]:
        labels = [_label(n) for n in observation.ui_tree if _label(n)]

        if profile and profile.page_signatures:
            best_page, best_score = self._match_profile(labels, profile)
            if best_page:
                return best_page, min(0.90, 0.70 + best_score * 0.05)

        if self._is_dialog(observation):
            return PageType.DIALOG.value, 0.75

        return self._match_rules(labels)

    def _match_profile(
        self,
        labels: list[str],
        profile: "ProjectProfile",
    ) -> tuple[str, int]:
        best_page = ""
        best_score = 0
        for page_name, keywords in profile.page_signatures.items():
            score = sum(
                1
                for kw in keywords
                if any(kw.strip().lower() in lb for lb in labels)
            )
            if score > best_score:
                best_score = score
                best_page = page_name
        return best_page, best_score

    @staticmethod
    def _is_dialog(observation: PageObservation) -> bool:
        clickable = observation.clickable_nodes
        if len(clickable) > 4:
            return False
        labels_lower = {_label(n) for n in clickable}
        dialog_hints = {"确认", "关闭", "确定", "取消", "ok", "close", "cancel"}
        return bool(labels_lower & dialog_hints)

    @staticmethod
    def _match_rules(labels: list[str]) -> tuple[str, float]:
        best_type = PageType.UNKNOWN.value
        best_conf = 0.0
        for page_type, keywords, base_conf in _PAGE_RULES:
            hits = sum(1 for kw in keywords if any(kw in lb for lb in labels))
            if hits > 0:
                conf = min(base_conf + hits * 0.03, 0.95)
                if conf > best_conf:
                    best_conf = conf
                    best_type = page_type
        return best_type, best_conf


# ---------------------------------------------------------------------------
# 控件语义角色分类器
# ---------------------------------------------------------------------------

_WIDGET_RULES: list[tuple[str, list[str], str, float]] = [
    (WidgetRole.CLOSE.value, ["关闭", "close", "×", "x_btn", "btn_close"], RiskLevel.NONE.value, 0.90),
    (WidgetRole.BACK.value, ["返回", "back", "btn_back", "return"], RiskLevel.NONE.value, 0.85),
    (WidgetRole.CONFIRM.value, ["确认", "确定", "ok", "confirm"], RiskLevel.NONE.value, 0.85),
    (WidgetRole.CANCEL.value, ["取消", "cancel"], RiskLevel.NONE.value, 0.80),
    (WidgetRole.SKIP.value, ["跳过", "skip"], RiskLevel.NONE.value, 0.80),
    (WidgetRole.REWARD_CLAIM.value, ["领取", "收下", "claim", "collect"], RiskLevel.NONE.value, 0.80),
    (WidgetRole.PRIMARY_ENTRY.value, ["开始", "进入", "play", "start", "enter", "下一步", "next"], RiskLevel.NONE.value, 0.75),
    (WidgetRole.BATTLE_START.value, ["战斗", "挑战", "出战", "battle", "fight"], RiskLevel.NONE.value, 0.70),
    (WidgetRole.BATTLE_AUTO.value, ["自动", "auto"], RiskLevel.NONE.value, 0.65),
    (WidgetRole.BATTLE_SETTLEMENT.value, ["结算", "settlement", "结果"], RiskLevel.NONE.value, 0.65),
    (WidgetRole.SHOP_ENTRY.value, ["商城", "商店", "shop", "store"], RiskLevel.LOW.value, 0.70),
    (WidgetRole.PAY_ENTRY.value, ["充值", "支付", "购买", "recharge", "pay"], RiskLevel.HIGH.value, 0.90),
]


class WidgetClassifier:
    """基于关键词规则的控件语义角色分类器。"""

    def __init__(self, dangerous_keywords: set[str] | None = None) -> None:
        self._dangerous = {kw.lower() for kw in (dangerous_keywords or set())}

    def classify(self, node: UiNode) -> tuple[str, str, float]:
        label = _label(node)
        if not label:
            return WidgetRole.UNKNOWN_ACTION.value, RiskLevel.NONE.value, 0.0

        if self._dangerous and any(kw in label for kw in self._dangerous):
            return WidgetRole.DANGEROUS_ACTION.value, RiskLevel.HIGH.value, 0.90

        for role, keywords, risk, conf in _WIDGET_RULES:
            if any(kw in label for kw in keywords):
                return role, risk, conf

        if node.clickable:
            if label.startswith("btn_") or "button" in label:
                return WidgetRole.SECONDARY_ENTRY.value, RiskLevel.NONE.value, 0.50
            return WidgetRole.UNKNOWN_ACTION.value, RiskLevel.NONE.value, 0.30

        return WidgetRole.UNKNOWN_ACTION.value, RiskLevel.NONE.value, 0.0

    def classify_page_nodes(
        self,
        observation: PageObservation,
    ) -> list[SemanticNode]:
        result: list[SemanticNode] = []
        for idx, node in enumerate(observation.clickable_nodes):
            role, risk, conf = self.classify(node)
            label = _label(node)
            if not label:
                continue
            result.append(
                SemanticNode(
                    node_id=f"sn_{idx}_{node.name or node.text or idx}",
                    page_signature=observation.page_signature,
                    raw_name=node.name,
                    raw_text=node.text,
                    normalized_label=label,
                    semantic_role=role,
                    risk_level=risk,
                    confidence=conf,
                    clickable=node.clickable,
                    visible=node.visible,
                    enabled=node.enabled,
                    bounds=node.bounds,
                )
            )
        return result


# ---------------------------------------------------------------------------
# 组合分析器
# ---------------------------------------------------------------------------


class SemanticAnalyzer:
    """组合页面分类和控件分类，输出语义化结果。"""

    def __init__(
        self,
        profile: "ProjectProfile | None" = None,
        dangerous_keywords: set[str] | None = None,
    ) -> None:
        self.page_classifier = PageClassifier()
        self.widget_classifier = WidgetClassifier(dangerous_keywords)
        self.profile = profile

    def analyze(
        self,
        observation: PageObservation,
    ) -> tuple[SemanticPageState, list[SemanticNode]]:
        page_type, confidence = self.page_classifier.classify(
            observation, self.profile
        )
        semantic_nodes = self.widget_classifier.classify_page_nodes(observation)

        risk_flags: list[str] = []
        for sn in semantic_nodes:
            if sn.risk_level in (RiskLevel.MEDIUM.value, RiskLevel.HIGH.value):
                risk_flags.append(f"{sn.semantic_role}:{sn.normalized_label}")

        key_texts = [n.text.strip() for n in observation.text_nodes if n.text.strip()][:8]
        key_clickables = [sn.normalized_label for sn in semantic_nodes][:12]
        root_features = [n.name for n in observation.root_nodes if n.name][:5]

        module_name = ""
        if page_type in (PageType.BATTLE_PREPARE.value, PageType.BATTLE_RUNNING.value, PageType.BATTLE_RESULT.value):
            module_name = "battle"
        elif page_type == PageType.SHOP.value:
            module_name = "shop"
        elif page_type == PageType.LOGIN.value:
            module_name = "login"
        elif page_type == PageType.LOBBY.value:
            module_name = "lobby"

        page_state = SemanticPageState(
            page_signature=observation.page_signature,
            canonical_page_name=observation.page_name_raw,
            page_type=page_type,
            module_name=module_name,
            semantic_tags=[page_type],
            confidence=confidence,
            root_features=root_features,
            key_texts=key_texts,
            key_clickables=key_clickables,
            risk_flags=risk_flags,
        )

        return page_state, semantic_nodes

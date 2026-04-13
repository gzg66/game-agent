"""观测层：页面快照采集、节点提取、签名生成。

职责：
- 采集 UI 树
- 提取可点击节点与文本节点
- 生成页面签名
- 输出结构化 PageObservation
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 节点信息
# ---------------------------------------------------------------------------

@dataclass
class ObservedNode:
    """从 UI 树中提取的单个节点信息。"""
    name: str
    text: str = ""
    node_type: str | None = None
    path: str = ""
    depth: int = 0
    clickable: bool = False
    visible: bool = True
    pos: list[float] | None = None
    size: list[float] | None = None
    components: list[str] = field(default_factory=list)
    interactive: bool = False
    candidate_score: float = 0.0
    candidate_reason: str = ""

    @property
    def label(self) -> str:
        """获取用于展示的标签。"""
        if self.name and self.text and self.text.lower() != self.name.lower():
            return f"{self.name} [{self.text}]"
        return self.text or self.name

    @property
    def action_key(self) -> str:
        """唯一标识该节点动作的 key。"""
        return "|".join([self.path, self.name, self.text, self.node_type or ""])


# ---------------------------------------------------------------------------
# 页面观测
# ---------------------------------------------------------------------------

@dataclass
class PageObservation:
    """一次页面观测的完整数据。

    对应冷启动设计文档中的 PageObservation。
    """
    signature: str
    title: str
    raw_hierarchy: dict[str, Any]
    all_nodes: list[ObservedNode]
    clickable_nodes: list[ObservedNode]
    actionable_candidates: list[ObservedNode]
    text_nodes: list[ObservedNode]
    root_node_name: str = ""
    captured_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str = ""  # 【新增】存储本次观测的截图路径


# ---------------------------------------------------------------------------
# 观测采集器
# ---------------------------------------------------------------------------

class ObservationCapture:
    """从 Poco 层级数据采集页面观测。

    不依赖具体引擎类型，只需要标准 Poco hierarchy dict。
    """

    @staticmethod
    def capture(hierarchy: dict[str, Any], screenshot_path: str = "") -> PageObservation: # 【修改】新增参数
        """对一个 hierarchy dump 执行全量观测。"""
        all_nodes = _extract_all_nodes(hierarchy)
        clickable = [n for n in all_nodes if n.interactive]
        actionable = _select_actionable_candidates(all_nodes)
        texts = [n for n in all_nodes if n.text]
        signature = _build_signature(hierarchy, all_nodes)
        title = _extract_title(all_nodes, hierarchy)
        root_name = _extract_root_name(hierarchy)

        return PageObservation(
            signature=signature,
            title=title,
            raw_hierarchy=hierarchy,
            all_nodes=all_nodes,
            clickable_nodes=clickable,
            actionable_candidates=actionable,
            text_nodes=texts,
            root_node_name=root_name,
            screenshot_path=screenshot_path, # 【修改】保存截图路径
        )


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _nested_text(node: dict[str, Any]) -> str:
    """递归提取节点或子节点的第一个文本。"""
    payload = node.get("payload", {}) or {}
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            child_text = _nested_text(child)
            if child_text:
                return child_text
    return ""


def _extract_all_nodes(hierarchy: dict[str, Any]) -> list[ObservedNode]:
    """把 hierarchy 树拍平成 ObservedNode 列表。"""
    nodes: list[ObservedNode] = []

    def walk(node: dict[str, Any], path: str = "root", depth: int = 0) -> None:
        payload = node.get("payload", {}) or {}
        name = str(node.get("name") or payload.get("name") or f"node_{len(nodes)}")
        text = _nested_text(node)
        components = payload.get("components", []) or []
        node_type = payload.get("type")
        clickable = bool(payload.get("clickable", False))
        visible = bool(payload.get("visible", True))
        pos = payload.get("pos")
        size = payload.get("size")

        interactive = bool(
            clickable
            or node_type in {"Button", "InputField", "Toggle", "Slider", "Dropdown"}
            or any(c in {"Button", "InputField", "StrongFeedback"} for c in components)
        )

        nodes.append(ObservedNode(
            name=name,
            text=text,
            node_type=node_type,
            path=path,
            depth=depth,
            clickable=clickable,
            visible=visible,
            pos=pos if isinstance(pos, list) else None,
            size=size if isinstance(size, list) else None,
            components=list(components),
            interactive=interactive,
        ))

        for idx, child in enumerate(node.get("children", []) or []):
            if isinstance(child, dict):
                child_name = str(child.get("name") or (child.get("payload", {}) or {}).get("name") or idx)
                walk(child, f"{path}/{idx}:{child_name}", depth + 1)

    walk(hierarchy)
    return nodes


def _select_actionable_candidates(nodes: list[ObservedNode]) -> list[ObservedNode]:
    """从全量节点中挑选可送入视觉/语义阶段的宽候选锚点。"""
    candidates: list[ObservedNode] = []
    for node in nodes:
        score, reason = _actionable_score(node)
        node.candidate_score = score
        node.candidate_reason = reason
        if score >= 2.0:
            candidates.append(node)

    candidates.sort(
        key=lambda n: (
            n.candidate_score,
            1 if n.clickable else 0,
            1 if n.interactive else 0,
            len((n.text or "").strip()),
            -n.depth,
        ),
        reverse=True,
    )
    return candidates


def _actionable_score(node: ObservedNode) -> tuple[float, str]:
    reasons: list[str] = []
    score = 0.0
    name = (node.name or "").strip().lower()
    text = (node.text or "").strip()
    node_type = (node.node_type or "").strip().lower()
    components = {str(component).lower() for component in node.components}

    if not node.visible:
        return 0.0, "invisible"

    if not _is_valid_normalized_rect(node.pos, node.size):
        return 0.0, "invalid_rect"

    if node.clickable:
        score += 5.0
        reasons.append("payload.clickable")
    if node.interactive:
        score += 4.0
        reasons.append("interactive_hint")

    if node_type in {"button", "inputfield", "editbox", "toggle", "slider", "dropdown"}:
        score += 3.0
        reasons.append(f"type={node.node_type}")
    elif node_type in {"component", "widget", "richtext", "label"}:
        score += 1.0
        reasons.append(f"broad_type={node.node_type}")

    matched_name_tokens = [token for token in _ACTIONABLE_NAME_TOKENS if token in name]
    if matched_name_tokens:
        score += 3.0
        reasons.append(f"name_tokens={','.join(matched_name_tokens[:3])}")

    matched_component_tokens = [token for token in _ACTIONABLE_COMPONENT_TOKENS if token in components]
    if matched_component_tokens:
        score += 3.0
        reasons.append(f"components={','.join(matched_component_tokens[:3])}")

    if text:
        score += 1.5
        reasons.append("has_text")
        if len(text) <= 12:
            score += 0.5
            reasons.append("compact_text")

    if node.depth >= 6:
        score += 0.5
        reasons.append("deep_node")

    if _looks_like_large_wrapper(node, name):
        score -= 4.0
        reasons.append("wrapper_penalty")

    if name in _NON_ACTIONABLE_EXACT_NAMES and not text:
        score -= 3.0
        reasons.append("non_actionable_exact")

    if score < 0:
        score = 0.0
    return score, "; ".join(reasons) if reasons else "weak_candidate"


def _build_signature(hierarchy: dict[str, Any], nodes: list[ObservedNode]) -> str:
    """基于节点结构生成页面签名（16 位 hex）。"""
    parts: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        payload = node.get("payload", {}) or {}
        name = str(node.get("name") or payload.get("name") or "")
        text = str(payload.get("text") or "")
        clickable = str(payload.get("clickable", False))
        if name or text:
            parts.append(f"{name}|{text}|{clickable}")
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(hierarchy)
    joined = "\n".join(parts[:80]).encode("utf-8", errors="ignore")
    return hashlib.sha1(joined).hexdigest()[:16]


def _extract_title(nodes: list[ObservedNode], hierarchy: dict[str, Any]) -> str:
    """提取页面标题，尽量避开账号、密码等字段值。"""
    best_text = ""
    best_score = float("-inf")

    for node in nodes:
        text = (node.text or "").strip()
        if not text or not node.visible:
            continue
        score = _title_score(node)
        if score > best_score:
            best_score = score
            best_text = text

    if best_text:
        return best_text

    text = _nested_text(hierarchy)
    return text if text else "unknown_page"


def _extract_root_name(hierarchy: dict[str, Any]) -> str:
    """提取根节点名称。"""
    payload = hierarchy.get("payload", {}) or {}
    return str(hierarchy.get("name") or payload.get("name") or "root")


_ACTIONABLE_NAME_TOKENS = {
    "btn",
    "button",
    "input",
    "tab",
    "icon",
    "item",
    "cell",
    "entry",
    "select",
    "toggle",
    "check",
    "drop",
}

_ACTIONABLE_COMPONENT_TOKENS = {
    "button",
    "inputfield",
    "strongfeedback",
    "toggle",
    "slider",
    "dropdown",
}

_NON_ACTIONABLE_EXACT_NAMES = {
    "scene",
    "canvas",
    "main camera",
    "ui camera",
    "groot",
    "container",
    "image",
}


def _is_valid_normalized_rect(
    pos: list[float] | None,
    size: list[float] | None,
) -> bool:
    if not isinstance(pos, list) or len(pos) != 2:
        return False
    x, y = pos
    if not (
        isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and 0.0 <= x <= 1.0
        and 0.0 <= y <= 1.0
    ):
        return False

    if not isinstance(size, list) or len(size) != 2:
        return False
    w, h = size
    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)):
        return False
    if w < 0 or h < 0:
        return False
    return not (w == 0 and h == 0)


def _looks_like_large_wrapper(node: ObservedNode, lowered_name: str) -> bool:
    if not node.size or len(node.size) != 2:
        return False
    w, h = node.size
    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)):
        return False
    wrapperish_name = (
        lowered_name in {"container", "root", "sceneui", "mainui", "contentgroup"}
        or lowered_name.endswith("layer")
        or lowered_name.endswith("root")
    )
    return wrapperish_name and w >= 0.9 and h >= 0.9


def _title_score(node: ObservedNode) -> float:
    text = (node.text or "").strip()
    lowered_name = (node.name or "").lower()
    score = 0.0

    if any(keyword in text for keyword in ("登录", "大厅", "公告", "提示", "奖励", "战斗")):
        score += 6.0
    if any(keyword in lowered_name for keyword in ("title", "header", "headline")):
        score += 3.0
    if any(char.isdigit() for char in text):
        score -= 3.0
    if "_" in text or len(text) > 18:
        score -= 2.0
    if text.startswith("请输入"):
        score -= 4.0
    if node.pos and len(node.pos) == 2:
        x, y = node.pos
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if 0.2 <= x <= 0.8:
                score += 1.0
            if y <= 0.4:
                score += 2.0
    if node.depth <= 6:
        score += 1.0
    return score

"""观测层：页面快照采集、节点提取、签名生成。

职责：
- 采集 UI 树
- 提取可点击节点与文本节点
- 生成页面签名
- 输出结构化 PageObservation
"""

from __future__ import annotations

import hashlib
import re
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
    normalized_signature: str = ""
    shell_signature: str = ""
    content_signature: str = ""
    overlay_signature: str = ""
    logical_page_key: str = ""
    normalized_actionable_paths: frozenset[str] = field(default_factory=frozenset)
    shell_actionable_paths: frozenset[str] = field(default_factory=frozenset)
    content_actionable_paths: frozenset[str] = field(default_factory=frozenset)
    overlay_actionable_paths: frozenset[str] = field(default_factory=frozenset)
    content_anchor_names: tuple[str, ...] = ()


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
        observation_features = _build_observation_features(all_nodes)
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
            normalized_signature=observation_features["normalized_signature"],
            shell_signature=observation_features["shell_signature"],
            content_signature=observation_features["content_signature"],
            overlay_signature=observation_features["overlay_signature"],
            logical_page_key=_build_logical_page_key(
                title=title,
                shell_signature=observation_features["shell_signature"],
                content_signature=observation_features["content_signature"],
                content_anchor_names=observation_features["content_anchor_names"],
                overlay_signature=observation_features["overlay_signature"],
            ),
            normalized_actionable_paths=observation_features["normalized_actionable_paths"],
            shell_actionable_paths=observation_features["shell_actionable_paths"],
            content_actionable_paths=observation_features["content_actionable_paths"],
            overlay_actionable_paths=observation_features["overlay_actionable_paths"],
            content_anchor_names=observation_features["content_anchor_names"],
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


def _stabilize_signature_text(text: str) -> str:
    """弱化战力、等级、倒计时等纯数字变化对签名的影响，减轻「同页却判跳转」。"""
    if not text:
        return ""
    t = text.strip()
    if len(t) > 64:
        t = t[:64]
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"#+", "#", t)
    return t.strip()


def _build_signature(hierarchy: dict[str, Any], nodes: list[ObservedNode]) -> str:
    """基于 UI 树生成页面签名（16 位 hex）。

    v4 版本会先对路径里的容器索引做归一化，降低 `Container/1`、`Container/2`
    这类壳层索引漂移带来的误判；同时保留节点文本与交互属性，避免把不同内容区
    粗暴折叠成同一页面。
    """
    lines = _signature_lines_from_nodes(nodes)
    if lines:
        return _hash_signature_lines("obs_sig_v4", lines)

    # 拍平结果异常为空时退回整树 DFS（无条数截断，text 用嵌套聚合）
    fb: list[str] = []

    def walk_fb(node: dict[str, Any]) -> None:
        payload = node.get("payload", {}) or {}
        if not payload.get("visible", True):
            return
        name = str(node.get("name") or payload.get("name") or "")
        text_fb = _stabilize_signature_text(_nested_text(node).strip())
        clickable = str(payload.get("clickable", False))
        if name or text_fb:
            fb.append(f"{name}|{text_fb}|{clickable}")
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk_fb(child)

    walk_fb(hierarchy)
    return _hash_signature_lines("obs_sig_v4_fb", fb)


def _build_observation_features(nodes: list[ObservedNode]) -> dict[str, Any]:
    signal_nodes = _signal_nodes(nodes)
    shell_nodes = [n for n in signal_nodes if _is_shell_path(n.path)]
    overlay_nodes = [n for n in signal_nodes if _is_overlay_path(n.path)]
    content_nodes = [
        n for n in signal_nodes
        if not _is_shell_path(n.path) and not _is_overlay_path(n.path)
    ]

    normalized_actionable_paths = _normalized_interactive_paths(signal_nodes)
    shell_actionable_paths = _normalized_interactive_paths(shell_nodes)
    content_actionable_paths = _normalized_interactive_paths(content_nodes)
    overlay_actionable_paths = _normalized_interactive_paths(overlay_nodes)
    content_anchor_names = _extract_content_anchor_names(signal_nodes)

    return {
        "normalized_signature": _hash_signature_lines(
            "obs_sig_v4_norm",
            _signature_lines_from_nodes(signal_nodes),
        ),
        "shell_signature": _optional_hash_signature_lines(
            "obs_shell_v1",
            _signature_lines_from_nodes(shell_nodes),
        ),
        "content_signature": _optional_hash_signature_lines(
            "obs_content_v1",
            _signature_lines_from_nodes(content_nodes),
        ),
        "overlay_signature": _optional_hash_signature_lines(
            "obs_overlay_v1",
            _signature_lines_from_nodes(overlay_nodes),
        ),
        "normalized_actionable_paths": normalized_actionable_paths,
        "shell_actionable_paths": shell_actionable_paths,
        "content_actionable_paths": content_actionable_paths,
        "overlay_actionable_paths": overlay_actionable_paths,
        "content_anchor_names": content_anchor_names,
    }


def _build_logical_page_key(
    title: str,
    shell_signature: str,
    content_signature: str,
    content_anchor_names: tuple[str, ...],
    overlay_signature: str,
) -> str:
    overlay_state = "overlay" if overlay_signature else "no_overlay"
    title_key = _stabilize_signature_text(title or "unknown_page")
    anchor_key = ",".join(content_anchor_names[:3]) if content_anchor_names else content_signature[:8]
    raw = "|".join([
        shell_signature[:12],
        title_key,
        anchor_key,
        overlay_state,
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _signal_nodes(nodes: list[ObservedNode]) -> list[ObservedNode]:
    return [
        n for n in nodes
        if n.visible and (n.clickable or n.interactive or (n.text or "").strip())
    ]


def _signature_lines_from_nodes(nodes: list[ObservedNode]) -> list[str]:
    lines: list[str] = []
    for node in nodes:
        if not node.visible:
            continue
        text = (node.text or "").strip()
        if not (node.clickable or node.interactive or text):
            continue
        lines.append(
            "|".join([
                _normalize_node_path(node.path),
                node.name,
                _stabilize_signature_text(text),
                str(int(node.clickable)),
                str(int(node.interactive)),
            ])
        )
    lines.sort()
    return lines


def _hash_signature_lines(prefix: str, lines: list[str]) -> str:
    joined = prefix + "\n" + "\n".join(lines)
    raw = joined.encode("utf-8", errors="ignore")
    if len(raw) > 300_000:
        raw = raw[:300_000]
    return hashlib.sha1(raw).hexdigest()[:16]


def _optional_hash_signature_lines(prefix: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return _hash_signature_lines(prefix, lines)


def _normalize_node_path(path: str) -> str:
    if not path:
        return ""
    return re.sub(r"/\d+:", "/#:", path)


def _normalized_interactive_paths(nodes: list[ObservedNode]) -> frozenset[str]:
    return frozenset(
        _normalize_node_path(node.path)
        for node in nodes
        if node.visible and (node.clickable or node.interactive)
    )


def _extract_content_anchor_names(nodes: list[ObservedNode]) -> tuple[str, ...]:
    anchors: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        for segment in _path_segments(node.path):
            lowered = segment.lower()
            if lowered in _IGNORED_ANCHOR_SEGMENTS:
                continue
            if lowered in _SHELL_SEGMENT_MARKERS_LOWER or lowered in _OVERLAY_SEGMENT_MARKERS_LOWER:
                continue
            if any(token in lowered for token in _CONTENT_ANCHOR_TOKENS):
                if segment not in seen:
                    seen.add(segment)
                    anchors.append(segment)
                    if len(anchors) >= 4:
                        return tuple(anchors)
    return tuple(anchors)


def _path_segments(path: str) -> list[str]:
    segments: list[str] = []
    for part in path.split("/"):
        if not part:
            continue
        if ":" in part:
            _, name = part.split(":", 1)
        else:
            name = part
        if name:
            segments.append(name)
    return segments


def _is_shell_path(path: str) -> bool:
    lowered = _normalize_node_path(path).lower()
    return any(marker in lowered for marker in _SHELL_PATH_MARKERS_LOWER)


def _is_overlay_path(path: str) -> bool:
    lowered = _normalize_node_path(path).lower()
    return any(marker in lowered for marker in _OVERLAY_PATH_MARKERS_LOWER)


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

_SHELL_PATH_MARKERS = (
    "MainSysBarView",
    "playerBar",
    "assetsBar",
    "functionBtnBar",
    "actionBtnList",
    "rightTopActionBtnList",
    "btnChat",
    "btnExpand",
)

_OVERLAY_PATH_MARKERS = (
    "PopWin",
    "mask",
    "Mask",
    "modal",
    "Modal",
    "Dialog",
    "dialog",
    "Toast",
    "toast",
    "Notice",
    "notice",
)

_CONTENT_ANCHOR_TOKENS = (
    "scene",
    "window",
    "panel",
    "view",
    "chapter",
    "bag",
    "battle",
    "play",
)

_IGNORED_ANCHOR_SEGMENTS = {
    "root",
    "container",
    "canvas",
    "groot",
    "scene",
    "mainui",
    "sceneui",
    "gcomponent",
    "gbutton",
    "gtextfield",
}

_SHELL_SEGMENT_MARKERS_LOWER = {marker.lower() for marker in _SHELL_PATH_MARKERS}
_OVERLAY_SEGMENT_MARKERS_LOWER = {marker.lower() for marker in _OVERLAY_PATH_MARKERS}
_SHELL_PATH_MARKERS_LOWER = tuple(marker.lower() for marker in _SHELL_PATH_MARKERS)
_OVERLAY_PATH_MARKERS_LOWER = tuple(marker.lower() for marker in _OVERLAY_PATH_MARKERS)


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

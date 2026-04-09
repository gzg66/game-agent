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
        texts = [n for n in all_nodes if n.text]
        signature = _build_signature(hierarchy, all_nodes)
        title = _extract_title(hierarchy)
        root_name = _extract_root_name(hierarchy)

        return PageObservation(
            signature=signature,
            title=title,
            raw_hierarchy=hierarchy,
            all_nodes=all_nodes,
            clickable_nodes=clickable,
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


def _extract_title(hierarchy: dict[str, Any]) -> str:
    """提取页面标题：取第一个有文本的节点。"""
    text = _nested_text(hierarchy)
    return text if text else "unknown_page"


def _extract_root_name(hierarchy: dict[str, Any]) -> str:
    """提取根节点名称。"""
    payload = hierarchy.get("payload", {}) or {}
    return str(hierarchy.get("name") or payload.get("name") or "root")

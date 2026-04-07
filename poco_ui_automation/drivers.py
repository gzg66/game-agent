from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .integration import EngineType
from .models import UiNode


@dataclass(slots=True)
class MockGraphState:
    name: str
    nodes: list[UiNode]
    transitions: dict[str, str]


class MockDriver:
    """用于本地验证 BFS、缓存和报告流程。"""

    def __init__(self, states: dict[str, MockGraphState], start_state: str) -> None:
        self.states = states
        self.current_state = start_state
        self.history: list[str] = []

    def freeze_nodes(self) -> list[UiNode]:
        return self.states[self.current_state].nodes

    def click(self, selector_query: str) -> bool:
        current = self.states[self.current_state]
        next_state = current.transitions.get(selector_query)
        if not next_state:
            return False
        self.history.append(self.current_state)
        self.current_state = next_state
        return True

    def back(self) -> bool:
        if not self.history:
            return False
        self.current_state = self.history.pop()
        return True

    def get_text(self, selector_query: str) -> str | None:
        for node in self.states[self.current_state].nodes:
            if node.name == selector_query or node.text == selector_query:
                return node.text
        return None

    def get_attr(self, selector_query: str, attr_name: str) -> Any:
        for node in self.states[self.current_state].nodes:
            if node.name == selector_query or node.text == selector_query:
                return node.attrs.get(attr_name)
        return None


class AirtestPocoDriver:
    """运行时驱动，依赖 airtest + pocoui。"""

    def __init__(
        self,
        engine_type: EngineType,
        device_uri: str | None = None,
        package_name: str | None = None,
        unity_addr: tuple[str, int] = ("localhost", 5001),
        cocos_addr: tuple[str, int] = ("localhost", 5003),
        std_port: int = 15004,
    ) -> None:
        from airtest.core.api import connect_device, keyevent

        self._keyevent = keyevent
        self.device = connect_device(device_uri) if device_uri else None
        self.poco = self._build_poco(engine_type, unity_addr, cocos_addr, std_port)
        self.package_name = package_name
        self.engine_type = engine_type

    def _build_poco(
        self,
        engine_type: EngineType,
        unity_addr: tuple[str, int],
        cocos_addr: tuple[str, int],
        std_port: int,
    ) -> Any:
        if engine_type == EngineType.UNITY3D:
            from poco.drivers.unity3d import UnityPoco

            return UnityPoco(addr=unity_addr, device=self.device)
        if engine_type in {EngineType.COCOS_CREATOR, EngineType.COCOS2DX_JS}:
            from poco.drivers.cocosjs import CocosJsPoco

            return CocosJsPoco(addr=cocos_addr, device=self.device)
        if engine_type == EngineType.COCOS2DX_LUA:
            from poco.drivers.std import StdPoco

            return StdPoco(port=std_port, device=self.device, use_airtest_input=True)

        from poco.drivers.android.uiautomation import AndroidUiautomationPoco

        return AndroidUiautomationPoco(use_airtest_input=True, screenshot_each_action=False)

    def freeze_nodes(self) -> list[UiNode]:
        freeze = self.poco.freeze()
        hierarchy = freeze.agent.hierarchy.dump()
        nodes: list[UiNode] = []
        self._flatten(hierarchy, nodes)
        return nodes

    def click(self, selector_query: str) -> bool:
        node = self._resolve_selector(selector_query)
        if not node.exists():
            return False
        node.click()
        return True

    def back(self) -> bool:
        self._keyevent("BACK")
        return True

    def get_text(self, selector_query: str) -> str | None:
        node = self._resolve_selector(selector_query)
        if not node.exists():
            return None
        try:
            return node.get_text()
        except Exception:
            return None

    def get_attr(self, selector_query: str, attr_name: str) -> Any:
        node = self._resolve_selector(selector_query)
        if not node.exists():
            return None
        try:
            return node.attr(attr_name)
        except Exception:
            return None

    def _resolve_selector(self, selector_query: str) -> Any:
        node = self.poco(selector_query)
        if node.exists():
            return node
        return self.poco(text=selector_query)

    def _flatten(self, raw_node: dict[str, Any], result: list[UiNode]) -> None:
        payload = raw_node.get("payload", {}) or {}
        name = str(raw_node.get("name") or raw_node.get("_name") or payload.get("name") or "")
        text = self._extract_text(raw_node, payload)
        visible = bool(raw_node.get("visible", payload.get("visible", True)))
        enabled = bool(raw_node.get("enabled", payload.get("enabled", True)))
        clickable = bool(raw_node.get("clickable", payload.get("clickable", True)))
        bounds = raw_node.get("pos") or payload.get("pos")
        result.append(
            UiNode(
                name=name,
                text=text,
                visible=visible,
                enabled=enabled,
                clickable=clickable,
                bounds=bounds if isinstance(bounds, tuple) else None,
                attrs=raw_node,
            )
        )
        for child in raw_node.get("children", []) or []:
            if isinstance(child, dict):
                self._flatten(child, result)

    def _extract_text(self, raw_node: dict[str, Any], payload: dict[str, Any]) -> str:
        text = raw_node.get("text") or raw_node.get("_text") or payload.get("text")
        if text:
            return str(text)
        for child in raw_node.get("children", []) or []:
            if not isinstance(child, dict):
                continue
            child_payload = child.get("payload", {}) or {}
            child_text = child.get("text") or child.get("_text") or child_payload.get("text")
            if child_text:
                return str(child_text)
        return ""

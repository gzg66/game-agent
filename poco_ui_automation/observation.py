from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .framework import DriverProtocol
from .models import DeviceContext, PageObservation, UiNode


class ObservationBuilder:
    """观测层：从原始 UI 树构建结构化 PageObservation。"""

    def __init__(
        self,
        session_id: str,
        device_context: DeviceContext | None = None,
    ) -> None:
        self.session_id = session_id
        self.device_context = device_context

    def observe(
        self,
        driver: DriverProtocol,
        step_index: int,
        screenshot_dir: Path | None = None,
    ) -> PageObservation:
        nodes = driver.freeze_nodes()
        root_nodes = nodes[:5]
        clickable_nodes = [n for n in nodes if n.clickable and n.visible and n.enabled]
        text_nodes = [n for n in nodes if n.text]
        signature = self.build_signature(nodes)
        page_name_raw = self._extract_page_name_raw(nodes)
        observation_id = f"obs_{step_index}_{int(time.time() * 1000)}"

        return PageObservation(
            observation_id=observation_id,
            session_id=self.session_id,
            step_index=step_index,
            page_signature=signature,
            page_name_raw=page_name_raw,
            ui_tree=nodes,
            root_nodes=root_nodes,
            clickable_nodes=clickable_nodes,
            text_nodes=text_nodes,
            screenshot_path=None,
            device_context=self.device_context,
            metadata={"node_count": len(nodes)},
        )

    @staticmethod
    def build_signature(nodes: list[UiNode]) -> str:
        clickable_names = sorted(
            {(n.name or n.text).strip() for n in nodes if n.clickable and (n.name or n.text)}
        )
        key_texts = sorted({n.text.strip() for n in nodes if n.text})[:8]
        payload: dict[str, Any] = {
            "roots": [n.name for n in nodes[:4]],
            "clickable": clickable_names[:12],
            "texts": key_texts,
            "bucket": len(nodes) // 10,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _extract_page_name_raw(nodes: list[UiNode]) -> str:
        for node in nodes:
            if node.text and node.text.strip():
                return node.text.strip()[:32]
        for node in nodes:
            name = (node.name or "").strip()
            if name and name.lower() not in ("root", "canvas", "uiroot"):
                return name[:32]
        return "unknown_page"

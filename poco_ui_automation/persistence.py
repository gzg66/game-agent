from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .models import (
    ActionExecution,
    AnomalySignal,
    ColdStartResult,
    IssueRecord,
    PageObservation,
    SemanticPageState,
    StateTransition,
)


def _serialize(obj: Any) -> str:
    return json.dumps(asdict(obj), ensure_ascii=False, default=str)


def _serialize_dict(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


class WorldModelStore:
    """世界模型持久化（JSON + JSONL）。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self._observations_path = self.output_dir / "observations.jsonl"
        self._actions_path = self.output_dir / "actions.jsonl"
        self._semantic_pages_path = self.output_dir / "semantic_pages.json"
        self._transitions_path = self.output_dir / "transitions.json"
        self._issues_path = self.output_dir / "issues.json"
        self._result_path = self.output_dir / "cold_start_result.json"

    def init_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def append_observation(self, obs: PageObservation) -> None:
        summary = {
            "observation_id": obs.observation_id,
            "session_id": obs.session_id,
            "step_index": obs.step_index,
            "page_signature": obs.page_signature,
            "page_name_raw": obs.page_name_raw,
            "node_count": len(obs.ui_tree),
            "clickable_count": len(obs.clickable_nodes),
            "text_count": len(obs.text_nodes),
            "captured_at": str(obs.captured_at),
        }
        self._append_jsonl(self._observations_path, summary)

    def append_action(self, execution: ActionExecution) -> None:
        self._append_jsonl(self._actions_path, asdict(execution))

    def save_semantic_pages(self, pages: dict[str, SemanticPageState]) -> None:
        data = {sig: asdict(page) for sig, page in pages.items()}
        self._write_json(self._semantic_pages_path, data)

    def save_transitions(self, transitions: dict[str, StateTransition]) -> None:
        data = {tid: asdict(t) for tid, t in transitions.items()}
        self._write_json(self._transitions_path, data)

    def save_issues(self, issues: list[IssueRecord]) -> None:
        data = [asdict(i) for i in issues]
        self._write_json(self._issues_path, data)

    def save_cold_start_result(self, result: ColdStartResult) -> None:
        self._write_json(self._result_path, asdict(result))

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

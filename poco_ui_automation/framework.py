from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Protocol

from .ai_strategy import HybridPlanner, RuleScenario, StateGraphMemory
from .cache import RefreshReason, UiStateCache
from .integration import ProjectProfile
from .metrics import MetricSampler
from .models import CoverageStats, ExecutedAction, PageSnapshot, RunSummary, Selector, UiNode
from .reporting import ReportBuilder


class DriverProtocol(Protocol):
    def freeze_nodes(self) -> list[UiNode]: ...

    def click(self, selector_query: str) -> bool: ...

    def back(self) -> bool: ...

    def get_text(self, selector_query: str) -> str | None: ...

    def get_attr(self, selector_query: str, attr_name: str) -> Any: ...


@dataclass(slots=True)
class SessionArtifacts:
    report_paths: dict[str, str]
    state_count: int
    edge_count: int
    summary: RunSummary


class AutomationSession:
    def __init__(
        self,
        profile: ProjectProfile,
        driver: DriverProtocol,
        output_dir: str | Path,
        planner: HybridPlanner | None = None,
        cache: UiStateCache | None = None,
        metrics: MetricSampler | None = None,
    ) -> None:
        self.profile = profile
        self.driver = driver
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.planner = planner or HybridPlanner(profile.dangerous_actions)
        self.cache = cache or UiStateCache()
        self.metrics = metrics or MetricSampler()
        self.memory = StateGraphMemory()

    def crawl_ui_graph(
        self,
        goal: str,
        max_states: int = 30,
        scenario: RuleScenario | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionArtifacts:
        summary = RunSummary(project_name=self.profile.project_name, goal=goal)
        if metadata:
            summary.metadata.update(metadata)
        visited: set[str] = set()
        queue: deque[str] = deque()

        first_snapshot = self.refresh_snapshot(RefreshReason.INITIAL)
        queue.append(first_snapshot.signature)
        self.memory.remember_node(first_snapshot)

        while queue and len(visited) < max_states:
            current_signature = queue.popleft()
            current_state = self.cache.get(current_signature)
            if not current_state or current_signature in visited:
                continue
            visited.add(current_signature)
            candidates = self.planner.plan(current_state.snapshot, self.memory, goal, scenario)
            for candidate in candidates:
                before_signature = current_state.snapshot.signature
                start = time.perf_counter()
                selector = Selector(candidate.selector_key, candidate.selector_query)
                self.cache.remember_selector(before_signature, candidate.selector_key, selector)
                success = self.driver.click(candidate.selector_query)
                duration_ms = int((time.perf_counter() - start) * 1000)
                self.cache.mark_selector_result(before_signature, candidate.selector_key, success)
                after_snapshot = self.refresh_snapshot(
                    RefreshReason.PAGE_CHANGED if success else RefreshReason.TARGET_NOT_FOUND
                )
                self.memory.remember_node(after_snapshot)
                self.memory.remember_edge(
                    before_signature,
                    after_snapshot.signature,
                    candidate.action_type,
                    candidate.selector_key,
                    duration_ms,
                    success,
                )
                summary.actions.append(
                    ExecutedAction(
                        index=len(summary.actions) + 1,
                        action_type=candidate.action_type,
                        selector_key=candidate.selector_key,
                        selector_query=candidate.selector_query,
                        page_signature_before=before_signature,
                        page_signature_after=after_snapshot.signature,
                        outcome="success" if success else "failed",
                        reason=candidate.reason,
                        duration_ms=duration_ms,
                        refresh_reason=RefreshReason.PAGE_CHANGED if success else RefreshReason.TARGET_NOT_FOUND,
                    )
                )
                if after_snapshot.signature not in visited:
                    queue.append(after_snapshot.signature)
                self.driver.back()
                back_snapshot = self.refresh_snapshot(RefreshReason.PAGE_CHANGED)
                self.memory.remember_node(back_snapshot)

        summary.business_metrics.extend(self.metrics.business_metrics)
        summary.performance_samples.extend(self.metrics.performance_samples)
        covered_pages = sorted({node.page_name for node in self.memory.nodes.values() if node.page_name})
        critical_hit = sorted(set(self.profile.critical_pages).intersection(covered_pages))
        summary.coverage = CoverageStats(
            visited_pages=len(covered_pages),
            visited_signatures=len(self.memory.nodes),
            modules_run=1 if summary.metadata.get("module_name") else 0,
            critical_pages_total=len(self.profile.critical_pages),
            critical_pages_covered=len(critical_hit),
            path_count=len(self.memory.edges),
            action_count=len(summary.actions),
            successful_actions=sum(1 for item in summary.actions if item.outcome == "success"),
            module_coverage={str(summary.metadata.get("module_name") or "default"): len(covered_pages)},
            covered_pages=covered_pages,
        )
        summary.complete("completed")
        report_paths = ReportBuilder(self.output_dir).build(summary, self.memory, metadata=summary.metadata)
        return SessionArtifacts(
            report_paths=report_paths,
            state_count=len(self.memory.nodes),
            edge_count=len(self.memory.edges),
            summary=summary,
        )

    def refresh_snapshot(self, reason: str) -> PageSnapshot:
        nodes = self.driver.freeze_nodes()
        snapshot = PageSnapshot(
            signature=self._build_signature(nodes),
            page_name=self._detect_page_name(nodes),
            nodes=nodes,
            key_texts=[node.text for node in nodes if node.text][:8],
            root_names=[node.name for node in nodes[:5] if node.name],
            metadata={"refresh_reason": reason, "node_count": len(nodes)},
        )
        self.cache.upsert_snapshot(snapshot, reason)
        return snapshot

    def _build_signature(self, nodes: list[UiNode]) -> str:
        clickable_names = sorted({(node.name or node.text).strip() for node in nodes if node.clickable and (node.name or node.text)})
        key_texts = sorted({node.text.strip() for node in nodes if node.text})[:8]
        payload = {
            "roots": [node.name for node in nodes[:4]],
            "clickable": clickable_names[:12],
            "texts": key_texts,
            "bucket": len(nodes) // 10,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _detect_page_name(self, nodes: list[UiNode]) -> str:
        matched_page = self._match_profile_page(nodes)
        if matched_page:
            return matched_page
        for node in nodes:
            label = (node.text or node.name).strip()
            if label:
                return label[:32]
        return "unknown_page"

    def _match_profile_page(self, nodes: list[UiNode]) -> str | None:
        if not self.profile.page_signatures:
            return None
        labels = [f"{node.text} {node.name}".strip().lower() for node in nodes if node.text or node.name]
        best_page = None
        best_score = 0
        for page_name, keywords in self.profile.page_signatures.items():
            score = 0
            for keyword in keywords:
                raw = keyword.strip().lower()
                if not raw:
                    continue
                if any(raw in label for label in labels):
                    score += 1
            if score > best_score:
                best_score = score
                best_page = page_name
        return best_page if best_score > 0 else None

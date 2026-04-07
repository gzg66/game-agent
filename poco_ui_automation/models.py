from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class UiNode:
    name: str
    text: str = ""
    node_id: str | None = None
    visible: bool = True
    enabled: bool = True
    clickable: bool = True
    bounds: tuple[float, float, float, float] | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return self.text or self.name or (self.node_id or "")


@dataclass(slots=True)
class Selector:
    key: str
    query: str
    attrs: dict[str, Any] = field(default_factory=dict)
    fallback_queries: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PageSnapshot:
    signature: str
    page_name: str
    nodes: list[UiNode]
    captured_at: datetime = field(default_factory=utc_now)
    key_texts: list[str] = field(default_factory=list)
    root_names: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SelectorStats:
    selector: Selector
    success_count: int = 0
    fail_count: int = 0
    last_used_at: datetime | None = None


@dataclass(slots=True)
class PageState:
    snapshot: PageSnapshot
    cached_selectors: dict[str, SelectorStats] = field(default_factory=dict)
    stable_until: datetime | None = None
    last_refresh_reason: str | None = None


@dataclass(slots=True)
class ActionCandidate:
    action_type: str
    selector_key: str
    selector_query: str
    reason: str
    score: float
    confidence: float
    expected_page: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutedAction:
    index: int
    action_type: str
    selector_key: str
    selector_query: str
    page_signature_before: str
    page_signature_after: str | None
    outcome: str
    reason: str
    duration_ms: int
    refresh_reason: str | None = None
    screenshot_path: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class BusinessMetric:
    name: str
    value: Any
    source: str
    unit: str = ""
    delta: Any | None = None


@dataclass(slots=True)
class PerformanceSample:
    scope: str
    avg_frame_ms: float | None = None
    p90_frame_ms: float | None = None
    p95_frame_ms: float | None = None
    p99_frame_ms: float | None = None
    jank_ratio: float | None = None
    total_frames: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class StateNode:
    signature: str
    page_name: str
    key_nodes: list[str]
    key_metrics: dict[str, Any] = field(default_factory=dict)
    screenshot_hash: str | None = None
    ui_hash: str | None = None


@dataclass(slots=True)
class StateEdge:
    from_signature: str
    to_signature: str
    action_type: str
    selector_key: str
    duration_ms: int
    success: bool
    performance: dict[str, Any] = field(default_factory=dict)
    count: int = 1


@dataclass(slots=True)
class IssueRecord:
    category: str
    severity: str
    title: str
    page_name: str
    page_signature: str | None = None
    action_index: int | None = None
    action_label: str | None = None
    reproduction_path: list[str] = field(default_factory=list)
    related_logs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class CoverageStats:
    visited_pages: int = 0
    visited_signatures: int = 0
    modules_run: int = 0
    critical_pages_total: int = 0
    critical_pages_covered: int = 0
    path_count: int = 0
    action_count: int = 0
    successful_actions: int = 0
    module_coverage: dict[str, int] = field(default_factory=dict)
    covered_pages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunSummary:
    project_name: str
    goal: str
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: str = "running"
    actions: list[ExecutedAction] = field(default_factory=list)
    issues: list[IssueRecord] = field(default_factory=list)
    business_metrics: list[BusinessMetric] = field(default_factory=list)
    performance_samples: list[PerformanceSample] = field(default_factory=list)
    coverage: CoverageStats | None = None
    ai_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def complete(self, status: str) -> None:
        self.status = status
        self.finished_at = utc_now()

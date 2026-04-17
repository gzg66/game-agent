from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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


# ---------------------------------------------------------------------------
# 冷启动世界模型：枚举与数据结构
# ---------------------------------------------------------------------------


class PageType(str, Enum):
    LOGIN = "login"
    LOBBY = "lobby"
    DIALOG = "dialog"
    MODULE_ENTRY = "module_entry"
    REWARD = "reward"
    BATTLE_PREPARE = "battle_prepare"
    BATTLE_RUNNING = "battle_running"
    BATTLE_RESULT = "battle_result"
    GUIDE = "guide"
    SHOP = "shop"
    UNKNOWN = "unknown"


class WidgetRole(str, Enum):
    PRIMARY_ENTRY = "primary_entry"
    SECONDARY_ENTRY = "secondary_entry"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    CLOSE = "close"
    BACK = "back"
    SKIP = "skip"
    REWARD_CLAIM = "reward_claim"
    BATTLE_START = "battle_start"
    BATTLE_AUTO = "battle_auto"
    BATTLE_SETTLEMENT = "battle_settlement"
    SHOP_ENTRY = "shop_entry"
    PAY_ENTRY = "pay_entry"
    DANGEROUS_ACTION = "dangerous_action"
    UNKNOWN_ACTION = "unknown_action"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionSource(str, Enum):
    UI_TREE = "ui_tree"
    HISTORY = "history"
    TEMPLATE = "template"
    RECOVERED = "recovered"


class AnomalyType(str, Enum):
    PROCESS_EXIT = "process_exit"
    DISCONNECT = "disconnect"
    LONG_STAY = "long_stay"
    REPEAT_STATE = "repeat_state"
    ACTION_NO_EFFECT = "action_no_effect"
    LOG_ERROR = "log_error"
    SUSPICIOUS_NUMBER = "suspicious_number"
    MISSING_RESOURCE = "missing_resource"


class ControlSignal(str, Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    BACKTRACK = "backtrack"
    REOBSERVE = "reobserve"
    SWITCH_MODULE = "switch_module"
    STOP = "stop"


# ---------------------------------------------------------------------------
# 观测层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeviceContext:
    device_uri: str
    package_name: str
    screen_size: tuple[int, int] | None = None
    orientation: str = "portrait"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PageObservation:
    observation_id: str
    session_id: str
    step_index: int
    page_signature: str
    page_name_raw: str
    ui_tree: list[UiNode]
    root_nodes: list[UiNode]
    clickable_nodes: list[UiNode]
    text_nodes: list[UiNode]
    screenshot_path: str | None = None
    captured_at: datetime = field(default_factory=utc_now)
    device_context: DeviceContext | None = None
    log_slice_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 语义层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SemanticPageState:
    page_signature: str
    canonical_page_name: str
    page_type: str
    module_name: str = ""
    semantic_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    root_features: list[str] = field(default_factory=list)
    key_texts: list[str] = field(default_factory=list)
    key_clickables: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    first_seen_at: datetime = field(default_factory=utc_now)
    last_seen_at: datetime = field(default_factory=utc_now)
    seen_count: int = 1


@dataclass(slots=True)
class SemanticNode:
    node_id: str
    page_signature: str
    raw_name: str
    raw_text: str
    normalized_label: str
    semantic_role: str
    risk_level: str = "none"
    confidence: float = 0.0
    clickable: bool = True
    visible: bool = True
    enabled: bool = True
    bounds: tuple[float, float, float, float] | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 动作层（与现有 ActionCandidate 共存，不冲突）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CandidateAction:
    action_id: str
    page_signature: str
    selector_query: str
    target_node_id: str
    semantic_intent: str
    semantic_role: str
    reason: str
    source: str = "ui_tree"
    risk_level: str = "none"
    priority_score: float = 0.0
    expected_result: str = ""
    fallback_queries: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActionExecution:
    execution_id: str
    session_id: str
    step_index: int
    action_id: str
    selector_query: str
    semantic_intent: str
    page_signature_before: str
    page_signature_after: str | None = None
    page_name_before: str = ""
    page_name_after: str = ""
    success: bool = False
    state_changed: bool = False
    duration_ms: int = 0
    diff_summary: str = ""
    timestamp: datetime = field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# 图谱层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StateTransition:
    transition_id: str
    from_signature: str
    to_signature: str
    action_id: str
    action_key: str
    semantic_intent: str = ""
    success: bool = True
    count: int = 1
    avg_duration_ms: float = 0.0
    module_name: str = ""
    last_seen_at: datetime = field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# 异常层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AnomalySignal:
    signal_type: str
    source: str
    severity_hint: str = "low"
    score: float = 0.0
    page_signature: str = ""
    page_name: str = ""
    step_index: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    captured_at: datetime = field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# 冷启动结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ColdStartResult:
    session_id: str
    project_name: str
    new_pages_count: int = 0
    merged_pages_count: int = 0
    module_entries: list[str] = field(default_factory=list)
    widget_semantic_tags: dict[str, int] = field(default_factory=dict)
    high_value_paths: list[list[str]] = field(default_factory=list)
    risk_areas: list[str] = field(default_factory=list)
    anomalies: list[AnomalySignal] = field(default_factory=list)
    issues: list[IssueRecord] = field(default_factory=list)
    total_steps: int = 0
    total_actions: int = 0
    stop_reason: str = ""
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

"""Poco + Cocos UI 自动化测试底座。"""

from .ai_strategy import HybridPlanner, RuleScenario, StateGraphMemory
from .anomaly import AnomalyDetector, AnomalyDetectorConfig
from .cache import RefreshReason, UiStateCache
from .candidate_gen import CandidateGenerator
from .cold_start import ColdStartConfig, ColdStartExplorer
from .drivers import AirtestPocoDriver, MockDriver, MockGraphState
from .framework import AutomationSession, DriverProtocol, SessionArtifacts
from .integration import EngineType, IntegrationRegistry, ProjectProfile
from .metrics import AndroidFrameParsers, MetricSampler
from .models import (
    ActionExecution,
    ActionSource,
    AnomalySignal,
    AnomalyType,
    CandidateAction,
    ColdStartResult,
    ControlSignal,
    DeviceContext,
    PageObservation,
    PageType,
    RiskLevel,
    SemanticNode,
    SemanticPageState,
    StateTransition,
    WidgetRole,
)
from .observation import ObservationBuilder
from .persistence import WorldModelStore
from .reporting import ReportBuilder
from .semantic import PageClassifier, SemanticAnalyzer, WidgetClassifier

__all__ = [
    # 现有导出
    "AirtestPocoDriver",
    "AndroidFrameParsers",
    "AutomationSession",
    "DriverProtocol",
    "EngineType",
    "HybridPlanner",
    "IntegrationRegistry",
    "MetricSampler",
    "MockDriver",
    "MockGraphState",
    "ProjectProfile",
    "RefreshReason",
    "ReportBuilder",
    "RuleScenario",
    "SessionArtifacts",
    "StateGraphMemory",
    "UiStateCache",
    # 冷启动新增导出
    "ActionExecution",
    "ActionSource",
    "AnomalyDetector",
    "AnomalyDetectorConfig",
    "AnomalySignal",
    "AnomalyType",
    "CandidateAction",
    "CandidateGenerator",
    "ColdStartConfig",
    "ColdStartExplorer",
    "ColdStartResult",
    "ControlSignal",
    "DeviceContext",
    "ObservationBuilder",
    "PageClassifier",
    "PageObservation",
    "PageType",
    "RiskLevel",
    "SemanticAnalyzer",
    "SemanticNode",
    "SemanticPageState",
    "StateTransition",
    "WidgetClassifier",
    "WidgetRole",
    "WorldModelStore",
]

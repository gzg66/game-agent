"""Poco + Cocos UI 自动化测试底座。"""

from .ai_strategy import HybridPlanner, RuleScenario, StateGraphMemory
from .cache import RefreshReason, UiStateCache
from .drivers import AirtestPocoDriver, MockDriver, MockGraphState
from .framework import AutomationSession, DriverProtocol, SessionArtifacts
from .integration import EngineType, IntegrationRegistry, ProjectProfile
from .metrics import AndroidFrameParsers, MetricSampler
from .reporting import ReportBuilder

__all__ = [
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
]

"""冷启动探索子系统。

通过低风险、结构化、可回放的方式，为 AI 建立第一版可用世界模型。

核心组件：
- GameConfig          统一游戏配置，支持多引擎快速切换
- ObservationCapture  观测层：页面快照采集
- SemanticAnalyzer    语义层：页面/控件分类识别
- ColdStartActionPlanner  动作规划层：冷启动优先级排序
- ExplorationGraph    图谱层：状态转移图沉淀
- ColdStartExplorer   探索器：主流程编排
- ColdStartReportBuilder  报告：冷启动验收报告
"""

from .action_planner import CandidateAction, ColdStartActionPlanner
from .config import (
    ENGINE_COCOS2DX_JS,
    ENGINE_COCOS2DX_LUA,
    ENGINE_COCOS_CREATOR,
    ENGINE_UNITY3D,
    GameConfig,
)
from .explorer import ColdStartExplorer, ColdStartResult, GameConnector
from .observation import ObservationCapture, ObservedNode, PageObservation
from .report import ColdStartReportBuilder
from .semantic import (
    ControlRole,
    NodeSemanticInfo,
    PageCategory,
    PageSemanticInfo,
    SemanticAnalyzer,
)
from .state_graph import ExplorationGraph, PageNode, TransitionEdge

__all__ = [
    # config
    "GameConfig",
    "ENGINE_UNITY3D",
    "ENGINE_COCOS_CREATOR",
    "ENGINE_COCOS2DX_JS",
    "ENGINE_COCOS2DX_LUA",
    # observation
    "ObservationCapture",
    "ObservedNode",
    "PageObservation",
    # semantic
    "PageCategory",
    "ControlRole",
    "NodeSemanticInfo",
    "PageSemanticInfo",
    "SemanticAnalyzer",
    # action planner
    "CandidateAction",
    "ColdStartActionPlanner",
    # state graph
    "ExplorationGraph",
    "PageNode",
    "TransitionEdge",
    # explorer
    "ColdStartExplorer",
    "ColdStartResult",
    "GameConnector",
    # report
    "ColdStartReportBuilder",
]

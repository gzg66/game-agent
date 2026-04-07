from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Any


class EngineType(str, Enum):
    UNITY3D = "unity3d"
    COCOS_CREATOR = "cocos_creator"
    COCOS2DX_JS = "cocos2dx_js"
    COCOS2DX_LUA = "cocos2dx_lua"


@dataclass(slots=True)
class IntegrationStandard:
    engine_type: EngineType
    sdk_path: str
    init_snippet: str
    required_runtime_steps: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectProfile:
    project_name: str
    engine_type: EngineType
    package_name: str
    ui_root_candidates: list[str]
    startup_wait_seconds: int = 8
    popup_blacklist: list[str] = field(default_factory=list)
    critical_pages: list[str] = field(default_factory=list)
    metric_sampling: dict[str, bool] = field(
        default_factory=lambda: {"fps": True, "stutter": True, "memory": False}
    )
    selector_aliases: dict[str, list[str]] = field(default_factory=dict)
    dangerous_actions: list[str] = field(default_factory=list)
    page_signatures: dict[str, list[str]] = field(default_factory=dict)
    module_scenarios: list[dict[str, Any]] = field(default_factory=list)
    runtime_guard: dict[str, Any] = field(default_factory=dict)
    issue_detection: dict[str, Any] = field(default_factory=dict)
    report_preferences: dict[str, Any] = field(default_factory=dict)
    log_keywords: list[str] = field(default_factory=list)
    initial_actions: list[str] = field(default_factory=list)
    version_tags: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.project_name:
            raise ValueError("project_name 不能为空")
        if not self.package_name:
            raise ValueError("package_name 不能为空")
        if not self.ui_root_candidates:
            raise ValueError("ui_root_candidates 至少配置一个根节点")

    @classmethod
    def load(cls, path: str | Path) -> "ProjectProfile":
        profile_path = Path(path)
        suffix = profile_path.suffix.lower()
        raw_text = profile_path.read_text(encoding="utf-8")
        if suffix == ".json":
            data = json.loads(raw_text)
        elif suffix in {".yaml", ".yml"}:
            data = _load_yaml_if_available(raw_text, profile_path)
        else:
            raise ValueError(f"不支持的配置格式: {profile_path.suffix}")
        data["engine_type"] = EngineType(data["engine_type"])
        profile = cls(**data)
        profile.validate()
        return profile


def _load_yaml_if_available(raw_text: str, path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"读取 {path.name} 需要 PyYAML。请先安装 pyyaml，或改用 JSON 配置。"
        ) from exc
    data = yaml.safe_load(raw_text)  # type: ignore[attr-defined]
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 根节点必须是对象")
    return data


class IntegrationRegistry:
    def __init__(self) -> None:
        self._items = {
            EngineType.UNITY3D: IntegrationStandard(
                engine_type=EngineType.UNITY3D,
                sdk_path="Poco-SDK/Unity3D",
                init_snippet=(
                    "Add Component -> Unity3D/PocoManager.cs\n"
                    "挂到 root 或 main camera 这类不会销毁的 GameObject 上"
                ),
                required_runtime_steps=[
                    "把 Unity3D SDK 拷到项目脚本目录。",
                    "按 UI 框架保留 ugui/ngui/fairygui 中对应目录。",
                    "把 PocoManager 挂到常驻 GameObject。",
                    "打包后使用 UnityPoco 连接默认 5001 端口。",
                ],
            ),
            EngineType.COCOS_CREATOR: IntegrationStandard(
                engine_type=EngineType.COCOS_CREATOR,
                sdk_path="Poco-SDK/cocos-creator/Poco",
                init_snippet=(
                    "onLoad: function () {\n"
                    "    var Poco = require('./Poco');\n"
                    "    window.poco = new Poco();\n"
                    "}"
                ),
                required_runtime_steps=[
                    "复制 Poco 目录到项目脚本目录。",
                    "开启 Cocos 引擎中的 WebSocketServer。",
                    "在常驻 onLoad 中初始化 window.poco。",
                    "打包后运行，并用 AirtestIDE 查看 Cocos-Js UI 树。",
                ],
                notes=[
                    "通常只能在打包后的 Android/Windows 版本上稳定使用。",
                ],
            ),
            EngineType.COCOS2DX_JS: IntegrationStandard(
                engine_type=EngineType.COCOS2DX_JS,
                sdk_path="Poco-SDK/cocos2dx-js/Poco",
                init_snippet=(
                    "var PocoManager = window.PocoManager;\n"
                    "var poco = new PocoManager();\n"
                    "window.poco = poco;"
                ),
                required_runtime_steps=[
                    "复制 Poco 目录到 JS 项目脚本目录。",
                    "注册 WebSocketServer native 模块与 JS 绑定。",
                    "把 Poco 相关脚本加入 project.json 的 jsList。",
                    "在初始化脚本挂载 window.poco。",
                ],
            ),
            EngineType.COCOS2DX_LUA: IntegrationStandard(
                engine_type=EngineType.COCOS2DX_LUA,
                sdk_path="Poco-SDK/cocos2dx-lua/poco",
                init_snippet=(
                    "local poco = require('poco.poco_manager')\n"
                    "poco:init_server(15004)"
                ),
                required_runtime_steps=[
                    "复制 poco 目录到 Lua 项目脚本目录。",
                    "确认 socket 或 socket.core 已启用。",
                    "在游戏初始化脚本中启动 tcp server。",
                    "查看日志确认 Poco 服务启动成功。",
                ],
            ),
        }

    def get(self, engine_type: EngineType) -> IntegrationStandard:
        return self._items[engine_type]

    def all(self) -> list[IntegrationStandard]:
        return list(self._items.values())

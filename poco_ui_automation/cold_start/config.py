"""冷启动探索的游戏配置模块。

通过统一配置数据类，支持 Unity、Cocos-JS 等不同游戏引擎的快速切换。
只需修改一份 YAML / JSON 配置即可更换游戏。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 引擎类型常量（与 integration.EngineType 对齐，但这里用纯字符串以降低耦合）
# ---------------------------------------------------------------------------
ENGINE_UNITY3D = "unity3d"
ENGINE_COCOS_CREATOR = "cocos_creator"
ENGINE_COCOS2DX_JS = "cocos2dx_js"
ENGINE_COCOS2DX_LUA = "cocos2dx_lua"
ENGINE_ANDROID_UIAUTOMATION = "android_uiautomation"

# 引擎 -> 默认 Poco 端口映射
_DEFAULT_PORTS: dict[str, int] = {
    ENGINE_UNITY3D: 5001,
    ENGINE_COCOS_CREATOR: 5003,
    ENGINE_COCOS2DX_JS: 5003,
    ENGINE_COCOS2DX_LUA: 15004,
    ENGINE_ANDROID_UIAUTOMATION: 0,
}


@dataclass
class GameConfig:
    """冷启动探索的完整游戏配置。

    设计目标：只改本配置即可切换不同游戏 / 不同引擎 / 不同设备。
    """

    # ---- 游戏标识 ----
    project_name: str = "星途天城(poco)"
    engine_type: str = ENGINE_COCOS2DX_JS
    package_name: str = "com.xttc.release.poco"
    activity_name: str = "com.xttc.release.poco/org.cocos2dx.javascript.AppActivity"

    # ---- 设备 ----
    device_uri: str = "Android:///127.0.0.1:16384"
    device_serial: str = "127.0.0.1:16384"

    # ---- Poco 连接 ----
    poco_host: str = "127.0.0.1"
    poco_port: int = 5003

    # ---- 探索参数 ----
    max_steps: int = 200  # 最大探索步数
    max_pages: int = 30  # 最大页面数
    max_actions_per_page: int = 20  # 每页最多尝试的动作数
    boot_wait_s: float = 10.0  # 启动等待秒数
    action_wait_s: float = 2.0  # 每次动作后等待秒数
    no_new_page_limit: int = 10  # 连续无新页面步数，达到则停止

    # ---- 页面跳转判定（动作前后）----
    # 签名已不同，但可交互控件 path 集合仍高度重合时，视为同页抖动，不把 page_changed 置 True
    page_change_path_jaccard_suppress_above: float = 0.92
    page_change_shell_min_interactive_paths: int = 8
    page_change_max_interactive_path_delta: int = 15
    transition_in_place_path_jaccard_above: float = 0.92
    transition_in_place_path_delta_max: int = 6
    transition_content_switch_path_jaccard_below: float = 0.72
    go_back_accept_same_logical_page: bool = True

    # ---- 视觉主导模式 ----
    vision_mode: str = "rule_first"
    vision_max_candidates: int = 16
    vision_min_confidence: float = 0.55
    vision_allow_low_confidence: bool = False
    vision_max_calls_per_page: int = 1
    # vision_first 下：简单登录/标题页规则即可覆盖，跳过 SoM/LLM 以省延迟与费用
    vision_skip_llm_for_categories: list[str] = field(default_factory=lambda: ["login"])
    vision_skip_llm_min_page_category_confidence: float = 0.25
    # 全文（节点文案 + title）任一包含即跳过 LLM，用于「点击开始冒险」等未判成 login 的标题页
    vision_skip_llm_text_markers_any: list[str] = field(default_factory=lambda: [
        "账号登录", "点击开始冒险", "一键注册",
    ])

    # ---- 安全配置 ----
    dangerous_keywords: list[str] = field(default_factory=lambda: [
        "充值", "支付", "购买", "删除", "删除账号", "退出登录",
        "退出游戏", "退出", "登出",
        "recharge", "pay", "purchase", "delete", "quit", "logout",
    ])
    safe_priority_keywords: list[str] = field(default_factory=lambda: [
        "关闭", "确认", "确定", "下一步", "开始", "进入", "领取", "跳过", "返回",
        "大厅", "冒险", "出战", "挑战", "自动", "结算",
        "close", "confirm", "next", "start", "enter", "claim", "skip", "back", "ok",
    ])

    # 主界面底栏等「多页共用同一套控件」的路径片段；命中则探索记录跨 signature 去重，避免换 Tab 后签名变化又重复点同一 Tab
    shell_nav_path_markers: list[str] = field(default_factory=lambda: ["MainSysBarView"])

    # ---- 页面签名关键字（用于页面类型识别） ----
    page_type_hints: dict[str, list[str]] = field(default_factory=lambda: {
        "login": [
            "开始游戏", "游客登录", "账号登录", "手机登录", "密码登录",
            "登录", "login", "请输入账号", "输入账号", "注册账号",
            # 仍保留短词，由 semantic._page_type_keyword_matches 排除「账号:」类调试条
            "账号", "account",
        ],
        "lobby": [
            "大厅", "lobby", "主页", "home", "主界面", "据点", "基地", "营地",
            "冒险", "背包", "任务", "邮件",
        ],
        "role_select": ["选角", "角色选择", "角色", "role", "职业"],
        "dialog": ["弹窗", "dialog", "提示", "notice", "公告", "更新公告", "用户协议", "实名认证", "签到弹窗"],
        "guide": ["引导", "guide", "新手", "tutorial", "教程", "下一步"],
        "reward": ["奖励", "reward", "领取", "claim", "签到"],
        "battle_prepare": ["编队", "准备", "出战", "选择", "prepare", "挑战"],
        "battle_running": ["战斗", "battle", "fighting", "combat", "自动", "暂停", "跳过"],
        "battle_result": ["结算", "result", "胜利", "失败", "victory", "defeat", "再次挑战"],
        "shop": ["商店", "shop", "商城", "mall", "store", "充值", "recharge"],
    })

    # ---- 控件语义关键字 ----
    control_role_hints: dict[str, list[str]] = field(default_factory=lambda: {
        "close": ["close", "关闭", "x", "btn_close", "CloseBtn"],
        "back": ["back", "返回", "btn_back", "BackBtn"],
        "confirm": ["confirm", "确认", "确定", "ok", "btn_confirm", "ConfirmBtn"],
        "skip": ["skip", "跳过", "btn_skip", "SkipBtn"],
        "reward_claim": ["领取", "claim", "receive", "collect"],
        "primary_entry": ["开始", "开始游戏", "进入", "登录", "游客登录", "账号登录", "start", "enter", "play", "go"],
        "battle_start": ["战斗", "出战", "挑战", "battle", "fight", "challenge"],
        "battle_auto": ["自动", "auto"],
        "dangerous_action": ["充值", "支付", "购买", "删除", "recharge", "pay", "purchase", "delete"],
    })

    # ---- 输出 ----
    output_dir: str = "outputs/cold_start"

    def effective_poco_port(self) -> int:
        """获取实际使用的 Poco 端口。"""
        if self.poco_port > 0:
            return self.poco_port
        return _DEFAULT_PORTS.get(self.engine_type, 5001)

    def validate(self) -> None:
        """配置校验。"""
        if not self.project_name:
            raise ValueError("project_name 不能为空")
        if not self.package_name:
            raise ValueError("package_name 不能为空")
        if self.engine_type not in _DEFAULT_PORTS:
            raise ValueError(f"不支持的引擎类型: {self.engine_type}，"
                             f"可选: {list(_DEFAULT_PORTS.keys())}")
        if self.vision_mode not in {"rule_first", "vision_first"}:
            raise ValueError("vision_mode 仅支持 rule_first 或 vision_first")
        if self.vision_max_candidates <= 0:
            raise ValueError("vision_max_candidates 必须大于 0")
        if not 0.0 <= self.vision_min_confidence <= 1.0:
            raise ValueError("vision_min_confidence 必须位于 0 到 1 之间")
        if not 0.0 <= self.vision_skip_llm_min_page_category_confidence <= 1.0:
            raise ValueError("vision_skip_llm_min_page_category_confidence 必须位于 0 到 1 之间")
        if self.vision_max_calls_per_page <= 0:
            raise ValueError("vision_max_calls_per_page 必须大于 0")
        if not 0.5 <= self.page_change_path_jaccard_suppress_above <= 1.0:
            raise ValueError("page_change_path_jaccard_suppress_above 建议位于 0.5 到 1.0 之间")
        if not 0.5 <= self.transition_in_place_path_jaccard_above <= 1.0:
            raise ValueError("transition_in_place_path_jaccard_above 建议位于 0.5 到 1.0 之间")
        if self.transition_in_place_path_delta_max < 0:
            raise ValueError("transition_in_place_path_delta_max 不能为负数")
        if not 0.0 <= self.transition_content_switch_path_jaccard_below <= 1.0:
            raise ValueError("transition_content_switch_path_jaccard_below 必须位于 0 到 1 之间")

    # ---- 加载方法 ----
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameConfig:
        """从字典创建配置，支持嵌套结构和扁平结构。"""
        flat: dict[str, Any] = {}

        # 处理嵌套结构（YAML 友好格式）
        if "device" in data and isinstance(data["device"], dict):
            flat["device_uri"] = data["device"].get("uri", cls.device_uri)
            flat["device_serial"] = data["device"].get("serial", cls.device_serial)
        if "connection" in data and isinstance(data["connection"], dict):
            flat["poco_host"] = data["connection"].get("host", cls.poco_host)
            flat["poco_port"] = data["connection"].get("port", 0)
        if "exploration" in data and isinstance(data["exploration"], dict):
            for key in (
                "max_steps", "max_pages", "max_actions_per_page",
                "boot_wait_s", "action_wait_s", "no_new_page_limit",
                "page_change_path_jaccard_suppress_above",
                "page_change_shell_min_interactive_paths",
                "page_change_max_interactive_path_delta",
                "transition_in_place_path_jaccard_above",
                "transition_in_place_path_delta_max",
                "transition_content_switch_path_jaccard_below",
                "go_back_accept_same_logical_page",
            ):
                if key in data["exploration"]:
                    flat[key] = data["exploration"][key]
        if "vision" in data and isinstance(data["vision"], dict):
            for key in (
                "vision_mode",
                "vision_max_candidates",
                "vision_min_confidence",
                "vision_allow_low_confidence",
                "vision_max_calls_per_page",
                "vision_skip_llm_for_categories",
                "vision_skip_llm_min_page_category_confidence",
                "vision_skip_llm_text_markers_any",
            ):
                if key in data["vision"]:
                    flat[key] = data["vision"][key]
        if "safety" in data and isinstance(data["safety"], dict):
            if "dangerous_keywords" in data["safety"]:
                flat["dangerous_keywords"] = data["safety"]["dangerous_keywords"]
            if "safe_priority_keywords" in data["safety"]:
                flat["safe_priority_keywords"] = data["safety"]["safe_priority_keywords"]

        # 直接的顶层字段
        for key in (
            "project_name", "engine_type", "package_name", "activity_name",
            "device_uri", "device_serial", "poco_host", "poco_port",
            "max_steps", "max_pages", "max_actions_per_page",
            "boot_wait_s", "action_wait_s", "no_new_page_limit",
            "page_change_path_jaccard_suppress_above",
            "page_change_shell_min_interactive_paths",
            "page_change_max_interactive_path_delta",
            "transition_in_place_path_jaccard_above",
            "transition_in_place_path_delta_max",
            "transition_content_switch_path_jaccard_below",
            "go_back_accept_same_logical_page",
            "vision_mode", "vision_max_candidates", "vision_min_confidence",
            "vision_allow_low_confidence", "vision_max_calls_per_page",
            "vision_skip_llm_for_categories",
            "vision_skip_llm_min_page_category_confidence",
            "vision_skip_llm_text_markers_any",
            "dangerous_keywords", "safe_priority_keywords",
            "shell_nav_path_markers",
            "page_type_hints", "control_role_hints", "output_dir",
        ):
            if key in data and key not in flat:
                flat[key] = data[key]

        config = cls(**flat)
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> GameConfig:
        """从 YAML 或 JSON 文件加载配置。"""
        config_path = Path(path)
        raw_text = config_path.read_text(encoding="utf-8")
        suffix = config_path.suffix.lower()

        if suffix == ".json":
            data = json.loads(raw_text)
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    f"读取 {config_path.name} 需要 PyYAML。请先 pip install pyyaml"
                ) from exc
            data = yaml.safe_load(raw_text)
        else:
            raise ValueError(f"不支持的配置格式: {suffix}，仅支持 .json / .yaml / .yml")

        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return cls.from_dict(data)

"""探索器：冷启动探索的主流程编排。

编排观测层 → 语义层 → 动作规划层 → 执行 → 图谱层 的完整流程。
支持多种引擎（Unity / Cocos-JS / Cocos-Lua 等），通过 GameConfig 切换。

停止条件：
- 达到最大探索步数
- 达到最大页面数
- 连续多步无新增页面
- 命中严重异常（进程崩溃 / RPC 断开）
- 进入高风险区域
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .action_planner import CandidateAction, ColdStartActionPlanner, _path_matches_shell_nav
from .config import (
    ENGINE_ANDROID_UIAUTOMATION,
    ENGINE_COCOS2DX_JS,
    ENGINE_COCOS2DX_LUA,
    ENGINE_COCOS_CREATOR,
    ENGINE_UNITY3D,
    GameConfig,
)
from .observation import ObservationCapture, ObservedNode, PageObservation
from .state_graph import ExplorationGraph
from .semantic import ControlRole, NodeSemanticInfo, _keyword_matches_node  # 【修改】去掉了 SemanticAnalyzer
from .enhanced_semantic import EnhancedSemanticAnalyzer # 【新增】引入新的分析器


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_LOG_LOCK = threading.Lock()


def _log_event(log_path: Path, payload: dict[str, Any]) -> None:
    time_str = _utc_iso()
    
    # 1. 提取核心信息用于控制台格式化输出，增强人类可读性
    kind = payload.get("kind", "unknown").upper()
    msg = payload.get("msg", "")
    
    # 构建终端打印的额外信息（过滤掉基础字段，只展示关键数据）
    extra_info = {k: v for k, v in payload.items() if k not in ["kind", "msg"]}
    extra_str = f" | 附加数据: {extra_info}" if extra_info else ""
    
    # 在终端打印易读的日志
    print(f"[{time_str}] [{kind}] {msg}{extra_str}")

    # 2. 依然保留原始的 JSONL 文件写入逻辑（为了兼容数据分析）
    with _LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time_str, **payload}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 游戏连接器（封装 ADB + Poco 交互，按引擎类型动态选择）
# ---------------------------------------------------------------------------

class GameConnector:
    """负责设备交互和 Poco 连接，屏蔽引擎差异。

    只需更换 GameConfig 即可支持不同引擎。
    """

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self.poco: Any = None
        self.device: Any = None
        self._adb_path: str | None = None

    # ---- 初始化 ----

    def connect(self) -> None:
        """连接设备并初始化 Poco。"""
        from airtest.core.api import connect_device
        self.device = connect_device(self.config.device_uri)
        self.ensure_poco_forward()

        self.poco = self._create_poco()

    def reconnect(self) -> None:
        """重新连接 Poco（用于 RPC 断开后恢复）。"""
        from airtest.core.api import connect_device
        self.device = connect_device(self.config.device_uri)
        self.ensure_poco_forward()
        self.poco = self._create_poco()

    def _create_poco(self) -> Any:
        """根据引擎类型创建对应的 Poco 实例。"""
        engine = self.config.engine_type
        host = self.config.poco_host
        port = self.config.effective_poco_port()

        if engine == ENGINE_UNITY3D:
            from poco.drivers.unity3d import UnityPoco
            return UnityPoco(addr=(host, port), device=self.device)

        if engine in {ENGINE_COCOS_CREATOR, ENGINE_COCOS2DX_JS}:
            from poco.drivers.cocosjs import CocosJsPoco
            return CocosJsPoco(addr=(host, port), device=self.device)

        if engine == ENGINE_COCOS2DX_LUA:
            from poco.drivers.std import StdPoco
            return StdPoco(port=port, device=self.device, use_airtest_input=True)

        if engine == ENGINE_ANDROID_UIAUTOMATION:
            from poco.drivers.android.uiautomation import AndroidUiautomationPoco
            return AndroidUiautomationPoco(use_airtest_input=True, screenshot_each_action=False)

        # 兜底：Android 原生 UI
        from poco.drivers.android.uiautomation import AndroidUiautomationPoco
        return AndroidUiautomationPoco(use_airtest_input=True, screenshot_each_action=False)

    # ---- ADB 操作 ----

    def _get_adb(self) -> str:
        if self._adb_path:
            return self._adb_path
        import airtest
        adb_exe = Path(airtest.__file__).resolve().parent / "core" / "android" / "static" / "adb" / "windows" / "adb.exe"
        if adb_exe.exists():
            self._adb_path = str(adb_exe)
        else:
            self._adb_path = "adb"  # 依赖 PATH 中的 adb
        return self._adb_path

    def adb_cmd(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [self._get_adb(), "-s", self.config.device_serial, *args]
        return subprocess.run(
            command, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="ignore",
        )

    def adb_output(self, *args: str) -> str:
        result = self.adb_cmd(*args)
        return (result.stdout or result.stderr or "").strip()

    def force_stop_app(self) -> None:
        self.adb_cmd("shell", "am", "force-stop", self.config.package_name)

    def ensure_poco_forward(self) -> None:
        port = self.config.effective_poco_port()
        if port <= 0 or self.config.engine_type == ENGINE_ANDROID_UIAUTOMATION:
            return
        self.adb_cmd("forward", f"tcp:{port}", f"tcp:{port}")

    def resolve_launch_activity(self) -> str:
        output = self.adb_output(
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            self.config.package_name,
        )
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if "/" in candidate and self.config.package_name in candidate:
                return candidate
        return ""

    def current_focus(self) -> str:
        focus_dump = self.adb_output("shell", "dumpsys", "window")
        focus_lines = [
            line.strip()
            for line in focus_dump.splitlines()
            if "mCurrentFocus" in line or "mFocusedApp" in line
        ]
        return " | ".join(focus_lines)

    def _wait_for_app_ready(self, timeout_s: float = 8.0) -> bool:
        deadline = time.time() + max(timeout_s, 1.0)
        while time.time() < deadline:
            if self.app_is_running():
                focus = self.current_focus()
                if self.config.package_name in focus or not focus:
                    return True
            time.sleep(0.5)
        return False

    def start_app(self) -> None:
        launch_candidates: list[str] = []
        if self.config.activity_name:
            launch_candidates.append(self.config.activity_name)

        resolved_activity = self.resolve_launch_activity()
        if resolved_activity and resolved_activity not in launch_candidates:
            launch_candidates.append(resolved_activity)

        errors: list[str] = []
        for activity_name in launch_candidates:
            result = self.adb_cmd("shell", "am", "start", "-n", activity_name)
            if self._wait_for_app_ready():
                self.config.activity_name = activity_name
                return
            errors.append(
                f"am start -n {activity_name}: "
                f"{(result.stderr or result.stdout or 'unknown_error').strip()}"
            )

        monkey_result = self.adb_cmd(
            "shell",
            "monkey",
            "-p",
            self.config.package_name,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        if self._wait_for_app_ready():
            if resolved_activity:
                self.config.activity_name = resolved_activity
            return

        focus = self.current_focus() or "<unknown>"
        error_text = (monkey_result.stderr or monkey_result.stdout or "unknown_error").strip()
        joined_errors = "; ".join(errors) if errors else "no_explicit_activity"
        raise RuntimeError(
            "游戏启动失败: "
            f"{joined_errors}; monkey: {error_text}; current_focus: {focus}"
        )

    def press_back(self) -> None:
        self.adb_cmd("shell", "input", "keyevent", "4")

    def snapshot(self, save_path: str) -> bool:
        """【新增】通过 adb 截图并保存到本地"""
        try:
            temp_path = "/sdcard/temp_screen_agent.png"
            self.adb_cmd("shell", "screencap", "-p", temp_path)
            # pull 到本地
            subprocess.run([self._get_adb(), "-s", self.config.device_serial, "pull", temp_path, save_path], check=False, capture_output=True)
            return Path(save_path).exists()
        except Exception:
            return False

    def app_is_running(self) -> bool:
        result = self.adb_cmd("shell", "pidof", self.config.package_name)
        return bool(result.stdout.strip())

    def get_screen_size(self) -> tuple[int, int]:
        result = self.adb_cmd("shell", "wm", "size")
        output = result.stdout.strip()
        for token in output.replace("Physical size:", "").split():
            if "x" in token:
                w, h = token.split("x", 1)
                if w.isdigit() and h.isdigit():
                    return int(w), int(h)
        return 1440, 2560

    # ---- Poco 操作 ----

    def dump_hierarchy(self, retries: int = 3, wait_s: float = 1.0) -> dict[str, Any] | None:
        """获取 Poco UI 树，失败返回 None。"""
        for _ in range(retries):
            try:
                return self.poco.freeze().agent.hierarchy.dump()
            except Exception:
                time.sleep(wait_s)
        return None

    def click_node(self, name: str, pos: list[float] | None, screen_size: tuple[int, int]) -> tuple[bool, str]:
        """点击一个节点，优先用 Poco name，兜底用 ADB tap。"""
        # 先尝试 Poco 名称点击
        if name:
            try:
                node = self.poco(name)
                if node.exists():
                    node.click()
                    return True, f"poco:{name}"
            except Exception:
                pass

        # 兜底：ADB 坐标点击
        if pos and isinstance(pos, list) and len(pos) == 2:
            x, y = pos
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                px = max(1, min(screen_size[0] - 1, int(x * screen_size[0])))
                py = max(1, min(screen_size[1] - 1, int(y * screen_size[1])))
                result = self.adb_cmd("shell", "input", "tap", str(px), str(py))
                if result.returncode == 0:
                    return True, f"tap:{px},{py}"
                return False, "adb_tap_failed"

        return False, "no_valid_target"


# ---------------------------------------------------------------------------
# 探索执行记录
# ---------------------------------------------------------------------------

class TransitionDecision:
    """动作后页面变化的分类结果。"""

    def __init__(
        self,
        ui_changed: bool,
        page_changed: bool,
        requires_return: bool,
        handoff_context: bool,
        transition_type: str,
        confidence: float,
        reasons: list[str] | None = None,
        raw_signature_changed: bool = False,
    ) -> None:
        self.ui_changed = ui_changed
        self.page_changed = page_changed
        self.requires_return = requires_return
        self.handoff_context = handoff_context
        self.transition_type = transition_type
        self.confidence = confidence
        self.reasons = reasons or []
        self.raw_signature_changed = raw_signature_changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ui_changed": self.ui_changed,
            "page_changed": self.page_changed,
            "requires_return": self.requires_return,
            "handoff_context": self.handoff_context,
            "transition_type": self.transition_type,
            "transition_confidence": round(self.confidence, 3),
            "transition_reasons": list(self.reasons),
            "raw_signature_changed": self.raw_signature_changed,
        }


class ActionExecution:
    """一次动作执行的记录。"""

    def __init__(
        self,
        step: int,
        action: CandidateAction,
        page_before: str,
        page_after: str | None,
        after_screenshot_path: str,
        success: bool,
        page_changed: bool,
        ui_changed: bool,
        requires_return: bool,
        handoff_context: bool,
        transition_type: str,
        transition_confidence: float,
        transition_reasons: list[str],
        logical_page_before: str,
        logical_page_after: str,
        click_info: str,
        duration_ms: int,
        page_title_before: str = "",
        page_visit_index: int = 1,
        semantic_source_before_action: str = "rule",
        cache_hit_before_action: bool = False,
        llm_pending_for_page: bool = False,
        action_role_reason: str = "",
        blocked_reason: str = "",
        unlock_hint_text: str = "",
        unlock_condition: str = "",
    ) -> None:
        self.step = step
        self.action = action
        self.page_before = page_before
        self.page_after = page_after
        self.after_screenshot_path = after_screenshot_path
        self.success = success
        self.page_changed = page_changed
        self.ui_changed = ui_changed
        self.requires_return = requires_return
        self.handoff_context = handoff_context
        self.transition_type = transition_type
        self.transition_confidence = transition_confidence
        self.transition_reasons = transition_reasons
        self.logical_page_before = logical_page_before
        self.logical_page_after = logical_page_after
        self.click_info = click_info
        self.duration_ms = duration_ms
        self.page_title_before = page_title_before
        self.page_visit_index = page_visit_index
        self.semantic_source_before_action = semantic_source_before_action
        self.cache_hit_before_action = cache_hit_before_action
        self.llm_pending_for_page = llm_pending_for_page
        self.action_role_reason = action_role_reason
        self.blocked_reason = blocked_reason
        self.unlock_hint_text = unlock_hint_text
        self.unlock_condition = unlock_condition

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action_key": self.action.action_key,
            "action_label": self.action.label,
            "action_role": self.action.role.value,
            "action_role_reason": self.action_role_reason,
            "page_before": self.page_before,
            "page_title_before": self.page_title_before,
            "page_visit_index": self.page_visit_index,
            "page_after": self.page_after,
            "success": self.success,
            "ui_changed": self.ui_changed,
            "page_changed": self.page_changed,
            "requires_return": self.requires_return,
            "handoff_context": self.handoff_context,
            "transition_type": self.transition_type,
            "transition_confidence": round(self.transition_confidence, 3),
            "transition_reasons": self.transition_reasons,
            "logical_page_before": self.logical_page_before,
            "logical_page_after": self.logical_page_after,
            "click_info": self.click_info,
            "duration_ms": self.duration_ms,
            "semantic_source_before_action": self.semantic_source_before_action,
            "cache_hit_before_action": self.cache_hit_before_action,
            "llm_pending_for_page": self.llm_pending_for_page,
            "blocked_reason": self.blocked_reason,
            "unlock_hint_text": self.unlock_hint_text,
            "unlock_condition": self.unlock_condition,
        }


# ---------------------------------------------------------------------------
# 冷启动探索结果
# ---------------------------------------------------------------------------

class ColdStartResult:
    """冷启动探索的最终结果。"""

    def __init__(self) -> None:
        self.status: str = "running"
        self.stop_reason: str = ""
        self.total_steps: int = 0
        self.new_pages_found: int = 0
        self.executions: list[ActionExecution] = []
        self.crashes: list[dict[str, Any]] = []
        self.graph: ExplorationGraph = ExplorationGraph()
        self.page_semantics: dict[str, dict[str, Any]] = {}  # sig -> semantic summary
        self.semantic_stats: dict[str, Any] = {}
        self.started_at: str = _utc_iso()
        self.finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "total_steps": self.total_steps,
            "new_pages_found": self.new_pages_found,
            "execution_count": len(self.executions),
            "crash_count": len(self.crashes),
            "page_count": self.graph.page_count,
            "edge_count": self.graph.edge_count,
            "crashes": self.crashes,
            "page_semantics": self.page_semantics,
            "semantic_stats": self.semantic_stats,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# 冷启动探索器
# ---------------------------------------------------------------------------

class ColdStartExplorer:
    """冷启动探索的主控制器。

    完整编排六步流程：
    1. 运行前准备（重启游戏、建立连接）
    2. 首屏采集（首次页面观测）
    3. 页面聚类与语义识别
    4. 受控动作探索
    5. 状态图沉淀
    6. 结果验收与报告
    """

    def __init__(self, config: GameConfig, llm_client: Any = None) -> None: # 【修改】增加 llm_client 参数
        self.config = config
        self.connector = GameConnector(config)
        self.observer = ObservationCapture()
        self.semantic = EnhancedSemanticAnalyzer(config, llm_client, event_callback=self._log_semantic_event)
        self.planner = ColdStartActionPlanner(config)
        self.result = ColdStartResult()

        # 输出目录
        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir = self.output_dir / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / "logs" / "exploration.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            self.log_path.unlink()

        # 运行时状态
        self._screen_size: tuple[int, int] = (1440, 2560)
        self._step_counter: int = 0
        self._consecutive_no_new: int = 0
        self._explored_pages: set[str] = set()
        self._page_visit_counts: dict[str, int] = {}

    # ================================================================
    # 主入口
    # ================================================================

    def run(self) -> ColdStartResult:
        """执行完整的冷启动探索流程。"""
        try:
            # 步骤 1：运行前准备
            self._prepare()

            # 步骤 2：首屏采集
            first_obs = self._capture_first_screen()
            if first_obs is None:
                self.result.status = "failed"
                self.result.stop_reason = "首屏采集失败"
                return self.result

            # 步骤 3 + 4 + 5：页面语义识别 → 受控探索 → 状态图沉淀
            self._explore_from(first_obs, depth=0, path_stack=[])

            # 步骤 6：结果验收
            if self.result.status == "running":
                self.result.status = "completed"
                if not self.result.stop_reason:
                    self.result.stop_reason = "exploration_completed"

        except Exception as exc:
            self.result.status = "error"
            self.result.stop_reason = f"unexpected_error: {exc}"
            _log_event(self.log_path, {"kind": "error", "error": str(exc)})

        finally:
            self.result.total_steps = self._step_counter
            self.result.finished_at = _utc_iso()
            self.semantic.shutdown(wait=True)
            self.result.semantic_stats = self.semantic.get_stats()
            self._save_outputs()

        return self.result

    # ================================================================
    # 步骤 1：运行前准备
    # ================================================================

    def _prepare(self) -> None:
            """重启游戏、建立连接、记录环境信息。"""
            _log_event(self.log_path, {
                "kind": "prepare_start",
                "msg": "开始冷启动前置准备：准备重启游戏进程...",  # 【新增】易读描述
                "project": self.config.project_name,
                "engine": self.config.engine_type,
                "package": self.config.package_name,
            })

            # 重启游戏
            self.connector.force_stop_app()
            time.sleep(1)
            self.connector.start_app()
            time.sleep(self.config.boot_wait_s)

            # 建立连接
            self.connector.connect()
            self._screen_size = self.connector.get_screen_size()

            _log_event(self.log_path, {
                "kind": "prepare_done",
                "msg": "前置准备完成：设备已连接，UI树接口已就绪。",  # 【新增】易读描述
                "screen_size": list(self._screen_size),
            })

    # ================================================================
    # 步骤 2：首屏采集
    # ================================================================

    def _capture_first_screen(self) -> PageObservation | None:
            """采集首屏页面。"""
            hierarchy = self.connector.dump_hierarchy(retries=5, wait_s=2.0)
            if hierarchy is None:
                _log_event(self.log_path, {
                    "kind": "first_screen_failed",
                    "msg": "首屏采集失败：无法获取 Poco UI 树数据。" # 【新增】易读描述
                })
                return None
                
            # 【新增】进行首屏截图
            screen_path = str(self.screenshot_dir / f"step_{self._step_counter}_init.png")
            if not self.connector.snapshot(screen_path):
                _log_event(self.log_path, {
                    "kind": "screenshot_failed",
                    "msg": "首屏截图失败，无法进行语义分析",
                    "step": self._step_counter,
                })
                return None

            obs = self.observer.capture(hierarchy, screenshot_path=screen_path)
            _log_event(self.log_path, {
                "kind": "first_screen",
                "msg": f"首屏采集成功：当前所在页面识别为 '{obs.title}'", # 【新增】易读描述
                "signature": obs.signature,
                "title": obs.title,
                "total_nodes": len(obs.all_nodes),
                "clickable_nodes": len(obs.clickable_nodes),
                "actionable_candidates": len(obs.actionable_candidates),
                "text_nodes": len(obs.text_nodes),
            })

            if self._looks_like_android_shell_only(obs):
                obs.metadata["ui_tree_not_exposed"] = True
                _log_event(self.log_path, {
                    "kind": "ui_tree_not_exposed",
                    "msg": "检测到当前仅能看到 Android 原生外壳节点，游戏画布内控件未暴露给 UIAutomator。",
                    "engine": self.config.engine_type,
                    "signature": obs.signature,
                    "total_nodes": len(obs.all_nodes),
                    "sample_node_names": [n.name for n in obs.all_nodes[:8]],
                    "suggestion": "请改用接入游戏引擎 Poco SDK 的构建，或补充基于截图/OCR 的兜底点击方案。",
                })
            return obs

    # ================================================================
    # 步骤 3/4/5：递归探索
    # ================================================================

    def _explore_from(
            self,
            observation: PageObservation,
            depth: int,
            path_stack: list[str],
        ) -> None:
            """从一个页面开始进行受控探索。"""
            sig = observation.signature

            # 防止循环
            if sig in path_stack:
                return

            # 检查停止条件
            if self._should_stop():
                return

            # ---- 语义分析 ----
            page_sem = self.semantic.analyze(observation)
            visit_count = self._page_visit_counts.get(sig, 0) + 1
            self._page_visit_counts[sig] = visit_count
            node_semantic_map = {
                sem.node.action_key: sem
                for sem in page_sem.node_semantics
            }

            # ---- 记录到状态图 ----
            page_node, is_new = self.result.graph.add_page(
                signature=sig,
                title=observation.title,
                category=page_sem.category.value,
                is_popup=page_sem.has_popup,
                is_high_risk=page_sem.has_high_risk,
                action_count=len(observation.actionable_candidates),
                step=self._step_counter,
            )

            if is_new:
                self.result.new_pages_found += 1
                self._consecutive_no_new = 0
            else:
                self._consecutive_no_new += 1

            # 记录页面语义
            self.result.page_semantics[sig] = {
                "page_id": page_node.page_id,
                "title": observation.title,
                "category": page_sem.category.value,
                "category_confidence": page_sem.category_confidence,
                "has_popup": page_sem.has_popup,
                "has_high_risk": page_sem.has_high_risk,
                "actionable_candidate_count": len(observation.actionable_candidates),
                "clickable_count": len(observation.clickable_nodes),
                "text_count": len(observation.text_nodes),
                "visit_count": visit_count,
                "semantic_source": page_sem.semantic_source,
                "cache_hit": page_sem.cache_hit,
                "llm_candidate_count": page_sem.llm_candidate_count,
                "llm_enriched_node_count": page_sem.llm_enriched_node_count,
                "llm_pending": page_sem.llm_pending,
                "blocked_action_count": page_sem.blocked_action_count,
                "blocked_actions": self._collect_blocked_actions(page_sem),
                "degraded_mode": page_sem.degraded_mode,
            }
            page_node.metadata["blocked_action_count"] = page_sem.blocked_action_count
            page_node.metadata["blocked_actions"] = self._collect_blocked_actions(page_sem)

            _log_event(self.log_path, {
                "kind": "page_analyzed",
                "msg": f"页面分析完成：识别为 '{page_sem.category.value}' 类别，是否新页面: {is_new}", # 【新增】
                "step": self._step_counter,
                "signature": sig,
                "page_id": page_node.page_id,
                "title": observation.title,
                "category": page_sem.category.value,
                "is_new": is_new,
                "depth": depth,
                "has_popup": page_sem.has_popup,
                "has_high_risk": page_sem.has_high_risk,
                "visit_count": visit_count,
                "semantic_source": page_sem.semantic_source,
                "cache_hit": page_sem.cache_hit,
                "llm_candidate_count": page_sem.llm_candidate_count,
                "llm_enriched_node_count": page_sem.llm_enriched_node_count,
                "llm_pending": page_sem.llm_pending,
                "actionable_candidate_count": len(observation.actionable_candidates),
                "degraded_mode": page_sem.degraded_mode,
            })

            # 【修改点 1：移除原本的“页面一波流”粗暴截断机制】
            # if sig in self._explored_pages:
            #     return
            # self._explored_pages.add(sig)

            # ---- 生成候选动作 ----
            candidates = self.planner.plan(page_sem)

            _log_event(self.log_path, {
                "kind": "actions_planned",
                "msg": f"动作规划完毕：当前页面共生成 {len(candidates)} 个安全候选动作", # 【新增】
                "signature": sig,
                "candidate_count": len(candidates),
                "candidates": [c.to_dict() for c in candidates[:5]],  # 前 5 个
                "semantic_source": page_sem.semantic_source,
                "cache_hit": page_sem.cache_hit,
                "degraded_mode": page_sem.degraded_mode,
            })

            # 【修改点 2：新增基于动作集的精细化退出判定】
            # 筛选出当前页面的安全动作（风险等级 < 2）
            safe_candidates = [c for c in candidates if c.risk_level < 2]

            # 如果没有安全动作，或者所有的安全动作都已经探索过了，才判定这个页面不需要继续驻留
            if not safe_candidates:
                if observation.metadata.get("ui_tree_not_exposed"):
                    self.result.status = "blocked"
                    self.result.stop_reason = "ui_tree_not_exposed_android_uiautomation"
                    _log_event(self.log_path, {
                        "kind": "stop",
                        "msg": "当前页面没有任何可操作节点，且检测到仅存在 Android 外壳层级，终止探索。",
                        "reason": self.result.stop_reason,
                        "signature": sig,
                        "engine": self.config.engine_type,
                    })
                elif not self.result.stop_reason:
                    self.result.stop_reason = "no_actionable_candidates"
                    _log_event(self.log_path, {
                        "kind": "stop",
                        "msg": "当前页面没有通过排序与置信度过滤的可执行动作，终止探索。",
                        "reason": self.result.stop_reason,
                        "signature": sig,
                        "semantic_source": page_sem.semantic_source,
                        "degraded_mode": page_sem.degraded_mode,
                    })
                return

            all_explored = all(
                self.planner.is_explored(sig, c.action_key, c.node) for c in safe_candidates
            )
            if all_explored:
                _log_event(self.log_path, {
                    "kind": "page_skipped_all_explored",
                    "signature": sig,
                    "msg": "当前页面所有安全的动作均已探索完毕"
                })
                return

            # ---- 逐个执行候选动作 ----
            for action in candidates:
                if self._should_stop():
                    break

                # 安全检查：跳过高风险
                if action.risk_level >= 2:
                    _log_event(self.log_path, {
                        "kind": "action_skipped_risk",
                        "msg": f"跳过危险动作：忽略 '{action.label}' (风险等级 {action.risk_level})", # 【新增】
                        "signature": sig,
                        "action_key": action.action_key,
                        "risk_level": action.risk_level,
                    })
                    continue
                
                # 【修改点 3：在循环中实时跳过已经尝试过的动作分支】
                if self.planner.is_explored(sig, action.action_key, action.node):
                    continue

                action_sem = node_semantic_map.get(action.action_key)
                execution = self._execute_action(
                    action,
                    observation,
                    sig,
                    observation.title,
                    visit_count,
                    page_sem.semantic_source,
                    page_sem.cache_hit,
                    page_sem.llm_pending,
                    action_sem,
                )
                if execution is None:
                    continue

                self.result.executions.append(execution)

                if execution.blocked_reason and action_sem is not None:
                    page_sem.blocked_action_count = sum(
                        1
                        for sem in page_sem.node_semantics
                        if sem.blocked_reason or sem.unlock_hint_text
                    )
                    blocked_actions = self._collect_blocked_actions(page_sem)
                    page_node.metadata["blocked_action_count"] = page_sem.blocked_action_count
                    page_node.metadata["blocked_actions"] = blocked_actions
                    page_summary = self.result.page_semantics.get(sig)
                    if page_summary is not None:
                        page_summary["blocked_action_count"] = page_sem.blocked_action_count
                        page_summary["blocked_actions"] = blocked_actions
                    cache = getattr(self.semantic, "cache", None)
                    if cache is not None:
                        cache.put(sig, page_sem)

                # 记录到状态图
                self.result.graph.add_transition(
                    from_sig=sig,
                    to_sig=execution.page_after or sig,
                    action_key=action.action_key,
                    action_label=action.label,
                    action_role=action.role.value,
                    success=execution.success,
                    page_changed=execution.page_changed,
                    risk_level=action.risk_level,
                    metadata={
                        "blocked_reason": execution.blocked_reason,
                        "unlock_hint_text": execution.unlock_hint_text,
                        "unlock_condition": execution.unlock_condition,
                        "ui_changed": execution.ui_changed,
                        "requires_return": execution.requires_return,
                        "handoff_context": execution.handoff_context,
                        "transition_type": execution.transition_type,
                        "transition_confidence": execution.transition_confidence,
                        "logical_page_before": execution.logical_page_before,
                        "logical_page_after": execution.logical_page_after,
                    },
                )

                self.planner.mark_explored(sig, action.action_key, action.node)

                # 【↓↓↓ 核心修复代码：插入在这里 ↓↓↓】
                # 回退短路机制：如果我们执行的是兜底的返回/关闭动作，并且成功发生了跳转，
                # 说明当前页面的生命周期已经自然结束，完成了向上一层的物理退回。
                # 绝对不能把它当成新分支去正向递归！
                if action.role in {ControlRole.BACK, ControlRole.CLOSE} and execution.page_changed:
                    _log_event(self.log_path, {
                        "kind": "natural_return",
                        "msg": "执行了回退动作，自然结束当前页面的探索",  # 【调整顺序】放到最前面
                        "signature": sig,
                        "action_key": action.action_key
                    })
                    break  # 直接跳出 for 循环，随着 DFS 函数的 return 自然交还控制权
                    # 【↑↑↑ 核心修复代码结束 ↑↑↑】

                # 如果页面变化了，根据跳转类型决定是“递归后回退”还是“切换上下文继续探索”
                if execution.page_changed and execution.page_after:
                    after_hierarchy = self.connector.dump_hierarchy()
                    if after_hierarchy:
                        after_obs = self.observer.capture(
                            after_hierarchy,
                            screenshot_path=execution.after_screenshot_path,
                        )
                        if execution.requires_return:
                            self._explore_from(
                                after_obs,
                                depth=depth + 1,
                                path_stack=[*path_stack, sig],
                            )

                            back_success = self._try_go_back(sig, observation.logical_page_key)
                            if not back_success:
                                current_hierarchy = self.connector.dump_hierarchy()
                                current_obs = self.observer.capture(current_hierarchy) if current_hierarchy else None
                                current_sig = current_obs.signature if current_obs else ""
                                current_logical = current_obs.logical_page_key if current_obs else ""

                                if not current_obs or not self._same_logical_page(sig, observation.logical_page_key, current_obs):
                                    _log_event(self.log_path, {
                                        "kind": "stranded_aborted",
                                        "msg": "回退失败，当前不在期望页面，终止该页面的剩余动作遍历",
                                        "expected_sig": sig,
                                        "expected_logical_page": observation.logical_page_key,
                                        "actual_sig": current_sig,
                                        "actual_logical_page": current_logical,
                                    })
                                    break
                        elif execution.handoff_context:
                            _log_event(self.log_path, {
                                "kind": "context_handoff",
                                "msg": "检测到上下文切换，交出旧上下文并从新页面继续探索",
                                "from_signature": sig,
                                "to_signature": after_obs.signature,
                                "transition_type": execution.transition_type,
                            })
                            self._explore_from(
                                after_obs,
                                depth=depth + 1,
                                path_stack=path_stack,
                            )
                            break

    # ================================================================
    # 动作执行
    # ================================================================

    def _should_suppress_page_change(
        self,
        before: PageObservation,
        after: PageObservation,
    ) -> bool:
        """签名不同但归一化后的可交互 path 集合几乎不变时，视为同页抖动。"""
        j_min = float(getattr(self.config, "page_change_path_jaccard_suppress_above", 0.92))
        min_p = int(getattr(self.config, "page_change_shell_min_interactive_paths", 8))
        max_delta = int(getattr(self.config, "page_change_max_interactive_path_delta", 15))

        pb, pa = before.normalized_actionable_paths, after.normalized_actionable_paths
        if len(pb) < min_p or len(pa) < min_p:
            return False
        jaccard, added, removed = self._path_diff_stats(pb, pa)
        return jaccard >= j_min and added <= max_delta and removed <= max_delta

    def _path_diff_stats(
        self,
        before_paths: frozenset[str],
        after_paths: frozenset[str],
    ) -> tuple[float, int, int]:
        union = before_paths | after_paths
        if not union:
            return 1.0, 0, 0
        inter = before_paths & after_paths
        added = len(after_paths - before_paths)
        removed = len(before_paths - after_paths)
        return len(inter) / len(union), added, removed

    def _same_logical_page(
        self,
        expected_sig: str,
        expected_logical_key: str,
        current_obs: PageObservation,
    ) -> bool:
        if current_obs.signature == expected_sig:
            return True
        if (
            getattr(self.config, "go_back_accept_same_logical_page", True)
            and expected_logical_key
            and current_obs.logical_page_key == expected_logical_key
        ):
            return True
        return False

    def _classify_transition(
        self,
        action: CandidateAction,
        before: PageObservation,
        after: PageObservation,
    ) -> TransitionDecision:
        raw_signature_changed = after.signature != before.signature
        ui_changed = raw_signature_changed or after.normalized_signature != before.normalized_signature
        shell_changed = after.shell_signature != before.shell_signature
        content_changed = after.content_signature != before.content_signature
        overlay_changed = after.overlay_signature != before.overlay_signature
        logical_page_changed = after.logical_page_key != before.logical_page_key
        overall_j, overall_added, overall_removed = self._path_diff_stats(
            before.normalized_actionable_paths,
            after.normalized_actionable_paths,
        )
        content_j, content_added, content_removed = self._path_diff_stats(
            before.content_actionable_paths,
            after.content_actionable_paths,
        )
        shell_j, shell_added, shell_removed = self._path_diff_stats(
            before.shell_actionable_paths,
            after.shell_actionable_paths,
        )
        is_shell_nav_action = _path_matches_shell_nav(action.node.path, self.config)
        in_place_j = float(getattr(self.config, "transition_in_place_path_jaccard_above", 0.92))
        in_place_delta = int(getattr(self.config, "transition_in_place_path_delta_max", 6))
        content_switch_cutoff = float(getattr(self.config, "transition_content_switch_path_jaccard_below", 0.72))
        reasons = [
            f"raw_sig={raw_signature_changed}",
            f"shell_changed={shell_changed}",
            f"content_changed={content_changed}",
            f"overlay_changed={overlay_changed}",
            f"overall_j={overall_j:.3f}",
            f"content_j={content_j:.3f}",
            f"shell_j={shell_j:.3f}",
            f"logical_changed={logical_page_changed}",
        ]

        if not ui_changed and not logical_page_changed:
            return TransitionDecision(
                ui_changed=False,
                page_changed=False,
                requires_return=False,
                handoff_context=False,
                transition_type="in_place",
                confidence=0.98,
                reasons=[*reasons, "no_core_feature_changed"],
                raw_signature_changed=raw_signature_changed,
            )

        if overlay_changed and not shell_changed and content_j >= max(0.75, content_switch_cutoff):
            overlay_type = "overlay_push"
            if len(after.overlay_actionable_paths) <= len(before.overlay_actionable_paths):
                overlay_type = "overlay_pop"
            return TransitionDecision(
                ui_changed=True,
                page_changed=True,
                requires_return=overlay_type == "overlay_push",
                handoff_context=overlay_type == "overlay_pop",
                transition_type=overlay_type,
                confidence=0.86,
                reasons=[*reasons, "overlay_signature_changed_with_stable_shell"],
                raw_signature_changed=raw_signature_changed,
            )

        if shell_changed and logical_page_changed and not is_shell_nav_action:
            return TransitionDecision(
                ui_changed=True,
                page_changed=True,
                requires_return=True,
                handoff_context=False,
                transition_type="full_navigation",
                confidence=0.84,
                reasons=[*reasons, "shell_and_logical_page_changed"],
                raw_signature_changed=raw_signature_changed,
            )

        if content_changed and not shell_changed:
            if content_j >= in_place_j and content_added <= in_place_delta and content_removed <= in_place_delta:
                return TransitionDecision(
                    ui_changed=True,
                    page_changed=False,
                    requires_return=False,
                    handoff_context=False,
                    transition_type="in_place",
                    confidence=0.82,
                    reasons=[*reasons, "content_high_overlap_same_shell"],
                    raw_signature_changed=raw_signature_changed,
                )
            return TransitionDecision(
                ui_changed=True,
                page_changed=True,
                requires_return=False,
                handoff_context=is_shell_nav_action,
                transition_type="hub_switch" if is_shell_nav_action else "content_switch",
                confidence=0.78,
                reasons=[*reasons, "content_changed_without_shell_change"],
                raw_signature_changed=raw_signature_changed,
            )

        if content_changed and shell_changed and is_shell_nav_action:
            return TransitionDecision(
                ui_changed=True,
                page_changed=True,
                requires_return=False,
                handoff_context=True,
                transition_type="hub_switch",
                confidence=0.8,
                reasons=[*reasons, "shell_nav_action_changed_shell_and_content"],
                raw_signature_changed=raw_signature_changed,
            )

        if self._should_suppress_page_change(before, after):
            return TransitionDecision(
                ui_changed=True,
                page_changed=False,
                requires_return=False,
                handoff_context=False,
                transition_type="in_place",
                confidence=0.75,
                reasons=[*reasons, "suppressed_by_normalized_path_overlap"],
                raw_signature_changed=raw_signature_changed,
            )

        if logical_page_changed and overall_j < content_switch_cutoff:
            return TransitionDecision(
                ui_changed=True,
                page_changed=True,
                requires_return=True,
                handoff_context=False,
                transition_type="full_navigation",
                confidence=0.68,
                reasons=[*reasons, "logical_page_changed_with_large_path_diff"],
                raw_signature_changed=raw_signature_changed,
            )

        return TransitionDecision(
            ui_changed=ui_changed,
            page_changed=False,
            requires_return=False,
            handoff_context=False,
            transition_type="unknown",
            confidence=0.45,
            reasons=[*reasons, f"added={overall_added}", f"removed={overall_removed}", f"shell_added={shell_added}", f"shell_removed={shell_removed}"],
            raw_signature_changed=raw_signature_changed,
        )

    def _execute_action(
        self,
        action: CandidateAction,
        current_observation: PageObservation,
        current_sig: str,
        current_title: str,
        page_visit_index: int,
        semantic_source: str,
        cache_hit: bool,
        llm_pending: bool,
        action_semantic: NodeSemanticInfo | None,
    ) -> ActionExecution | None:
        """执行一个候选动作并返回执行记录。"""
        self._step_counter += 1
        self.result.total_steps = self._step_counter

        _log_event(self.log_path, {
            "kind": "action_start",
            "msg": f"[{self._step_counter}步] 开始执行：点击 '{action.label}' (作用: {action.role.value})", # 【新增】
            "step": self._step_counter,
            "signature": current_sig,
            "action_key": action.action_key,
            "action_label": action.label,
            "action_role": action.role.value,
            "page_title": current_title,
            "page_visit_index": page_visit_index,
            "semantic_source": semantic_source,
            "cache_hit": cache_hit,
            "llm_pending": llm_pending,
        })

        start_time = time.perf_counter()

        # 执行点击
        ok, click_info = self.connector.click_node(
            action.node.name, action.node.pos, self._screen_size
        )

        if not ok:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            _log_event(self.log_path, {
                "kind": "action_failed",
                "msg": f"[{self._step_counter}步] 动作失败：{click_info}", # 【新增】
                "step": self._step_counter,
                "action_key": action.action_key,
                "reason": click_info,
            })
            return ActionExecution(
                step=self._step_counter,
                action=action,
                page_before=current_sig,
                page_after=None,
                after_screenshot_path="",
                success=False,
                page_changed=False,
                ui_changed=False,
                requires_return=False,
                handoff_context=False,
                transition_type="action_failed",
                transition_confidence=1.0,
                transition_reasons=["click_failed_before_observation"],
                logical_page_before=current_observation.logical_page_key,
                logical_page_after=current_observation.logical_page_key,
                click_info=click_info,
                duration_ms=duration_ms,
                page_title_before=current_title,
                page_visit_index=page_visit_index,
                semantic_source_before_action=semantic_source,
                cache_hit_before_action=cache_hit,
                llm_pending_for_page=llm_pending,
                action_role_reason=action_semantic.role_reason if action_semantic else "",
            )

        time.sleep(self.config.action_wait_s)
        duration_ms = int((time.perf_counter() - start_time) * 1000)

        # 检查应用是否崩溃
        if not self.connector.app_is_running():
            crash = {
                "step": self._step_counter,
                "signature": current_sig,
                "action_key": action.action_key,
                "crash_type": "process_disappeared",
            }
            self.result.crashes.append(crash)
            self.result.stop_reason = "app_crash"
            _log_event(self.log_path, {"kind": "app_crash", "msg": "🚨 严重异常：游戏进程崩溃消失！", **crash}) # 【新增 msg】
            return ActionExecution(
                step=self._step_counter,
                action=action,
                page_before=current_sig,
                page_after=None,
                after_screenshot_path="",
                success=False,
                page_changed=False,
                ui_changed=False,
                requires_return=False,
                handoff_context=False,
                transition_type="app_crash",
                transition_confidence=1.0,
                transition_reasons=["app_not_running_after_action"],
                logical_page_before=current_observation.logical_page_key,
                logical_page_after=current_observation.logical_page_key,
                click_info=click_info,
                duration_ms=duration_ms,
                page_title_before=current_title,
                page_visit_index=page_visit_index,
                semantic_source_before_action=semantic_source,
                cache_hit_before_action=cache_hit,
                llm_pending_for_page=llm_pending,
                action_role_reason=action_semantic.role_reason if action_semantic else "",
            )

        # 采集动作后的页面
        after_hierarchy = self.connector.dump_hierarchy()
        if after_hierarchy is None:
            # RPC 断开，尝试重连
            try:
                self.connector.reconnect()
                after_hierarchy = self.connector.dump_hierarchy()
            except Exception as exc:
                crash = {
                    "step": self._step_counter,
                    "signature": current_sig,
                    "action_key": action.action_key,
                    "crash_type": f"rpc_broken: {exc}",
                }
                self.result.crashes.append(crash)
                self.result.stop_reason = "rpc_broken"
                _log_event(self.log_path, {"kind": "rpc_broken", "msg": f"🚨 严重异常：Poco RPC 连接断开 - {exc}", **crash}) # 【新增 msg】
                return None

        if after_hierarchy is None:
            return None

        # 【新增】动作执行后采集截图
        screen_path = str(self.screenshot_dir / f"step_{self._step_counter}_after.png")
        self.connector.snapshot(screen_path)

        after_obs = self.observer.capture(after_hierarchy, screenshot_path=screen_path) # 【修改】传入截图
        transition = self._classify_transition(action, current_observation, after_obs)
        blocked_hint = self._detect_blocked_hint(
            action=action,
            before_obs=current_observation,
            after_obs=after_obs,
        ) if not transition.page_changed else {}
        blocked_reason = str(blocked_hint.get("blocked_reason", ""))
        unlock_hint_text = str(blocked_hint.get("unlock_hint_text", ""))
        unlock_condition = str(blocked_hint.get("unlock_condition", ""))

        if blocked_reason:
            after_obs.metadata["blocked_reason"] = blocked_reason
        if unlock_hint_text:
            after_obs.metadata["unlock_hint_text"] = unlock_hint_text
        if unlock_condition:
            after_obs.metadata["unlock_condition"] = unlock_condition

        if blocked_reason:
            action.blocked_reason = blocked_reason
            action.unlock_hint_text = unlock_hint_text
            action.unlock_condition = unlock_condition

        if blocked_reason and action_semantic is not None:
            action_semantic.blocked_reason = blocked_reason
            action_semantic.unlock_hint_text = unlock_hint_text
            action_semantic.unlock_condition = unlock_condition

        if transition.transition_type == "in_place" and transition.raw_signature_changed:
            _log_event(self.log_path, {
                "kind": "page_change_suppressed",
                "msg": "签名已变，但分类器判定为同页或页内抖动，不按新页面处理",
                "step": self._step_counter,
                "signature": current_sig,
                "after_signature": after_obs.signature,
                "action_key": action.action_key,
                "transition_type": transition.transition_type,
                "transition_reasons": transition.reasons,
            })

        _log_event(self.log_path, {
            "kind": "transition_classified",
            "step": self._step_counter,
            "signature": current_sig,
            "after_signature": after_obs.signature,
            "action_key": action.action_key,
            "logical_page_before": current_observation.logical_page_key,
            "logical_page_after": after_obs.logical_page_key,
            **transition.to_dict(),
        })

        _log_event(self.log_path, {
            "kind": "action_end",
            "msg": f"[{self._step_counter}步] 执行完毕：耗时 {duration_ms}ms，跳转类型 {transition.transition_type}，是否进入新上下文: {transition.page_changed}",
            "step": self._step_counter,
            "signature": current_sig,
            "action_key": action.action_key,
            "after_signature": after_obs.signature,
            "raw_signature_changed": transition.raw_signature_changed,
            "ui_changed": transition.ui_changed,
            "page_changed": transition.page_changed,
            "requires_return": transition.requires_return,
            "handoff_context": transition.handoff_context,
            "transition_type": transition.transition_type,
            "transition_confidence": transition.confidence,
            "transition_reasons": transition.reasons,
            "duration_ms": duration_ms,
            "blocked_reason": blocked_reason,
            "unlock_hint_text": unlock_hint_text,
            "unlock_condition": unlock_condition,
        })

        return ActionExecution(
            step=self._step_counter,
            action=action,
            page_before=current_sig,
            page_after=after_obs.signature,
            after_screenshot_path=screen_path,
            success=True,
            page_changed=transition.page_changed,
            ui_changed=transition.ui_changed,
            requires_return=transition.requires_return,
            handoff_context=transition.handoff_context,
            transition_type=transition.transition_type,
            transition_confidence=transition.confidence,
            transition_reasons=transition.reasons,
            logical_page_before=current_observation.logical_page_key,
            logical_page_after=after_obs.logical_page_key,
            click_info=click_info,
            duration_ms=duration_ms,
            page_title_before=current_title,
            page_visit_index=page_visit_index,
            semantic_source_before_action=semantic_source,
            cache_hit_before_action=cache_hit,
            llm_pending_for_page=llm_pending,
            action_role_reason=action_semantic.role_reason if action_semantic else "",
            blocked_reason=blocked_reason,
            unlock_hint_text=unlock_hint_text,
            unlock_condition=unlock_condition,
        )

    def _collect_blocked_actions(self, page_sem: Any) -> list[dict[str, str]]:
        blocked_actions: list[dict[str, str]] = []
        for sem in getattr(page_sem, "node_semantics", []):
            if not (sem.blocked_reason or sem.unlock_hint_text):
                continue
            blocked_actions.append({
                "action_key": sem.node.action_key,
                "label": sem.node.label,
                "blocked_reason": sem.blocked_reason,
                "unlock_hint_text": sem.unlock_hint_text,
                "unlock_condition": sem.unlock_condition,
            })
        return blocked_actions

    def _detect_blocked_hint(
        self,
        action: CandidateAction,
        before_obs: PageObservation,
        after_obs: PageObservation,
    ) -> dict[str, str]:
        before_texts = {
            self._normalize_hint_text(text)
            for text in self._candidate_hint_texts(before_obs)
        }
        after_texts = self._candidate_hint_texts(after_obs)

        candidate_texts = [
            text
            for text in after_texts
            if self._normalize_hint_text(text) not in before_texts
        ]

        for text in candidate_texts:
            parsed = self._parse_unlock_text(text)
            if parsed:
                return parsed

        for title in (after_obs.title, before_obs.title):
            cleaned_title = self._cleanup_hint_text(title)
            if len(cleaned_title) <= 2:
                continue
            parsed = self._parse_unlock_text(cleaned_title)
            if parsed:
                return parsed

        if (
            "解锁" in (after_obs.title or "")
            and action.role in {
                ControlRole.PRIMARY_ENTRY,
                ControlRole.REWARD_CLAIM,
                ControlRole.BATTLE_START,
            }
        ):
            return {
                "blocked_reason": "unlock_requirement_not_met",
                "unlock_hint_text": after_obs.title,
                "unlock_condition": "",
            }
        return {}

    def _detect_blocked_hint_from_screenshot(self, after_obs: PageObservation) -> dict[str, str]:
        screenshot_path = getattr(after_obs, "screenshot_path", "")
        if not screenshot_path:
            return {}

        vision_service = getattr(self.semantic, "vision_service", None)
        if vision_service is None:
            return {}

        hint_payload = vision_service.detect_blocked_hint(screenshot_path)
        if not hint_payload:
            return {}

        hint_text = self._cleanup_hint_text(str(hint_payload.get("hint_text", "")))
        if not hint_text:
            return {}

        parsed = self._parse_unlock_text(hint_text)
        if parsed:
            return parsed

        return {
            "blocked_reason": "unknown",
            "unlock_hint_text": hint_text,
            "unlock_condition": hint_text,
        }

    def _candidate_hint_texts(self, observation: PageObservation) -> list[str]:
        seen: set[str] = set()
        texts: list[str] = []
        ranked_nodes = sorted(
            observation.text_nodes,
            key=self._hint_node_priority,
            reverse=True,
        )

        for node in ranked_nodes[:120]:
            cleaned = self._cleanup_hint_text(node.text)
            normalized = self._normalize_hint_text(cleaned)
            if not cleaned or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            texts.append(cleaned)

        cleaned_title = self._cleanup_hint_text(observation.title)
        normalized_title = self._normalize_hint_text(cleaned_title)
        if cleaned_title and normalized_title and normalized_title not in seen:
            texts.append(cleaned_title)
        return texts

    def _hint_node_priority(self, node: ObservedNode) -> tuple[float, int, int]:
        score = 0.0
        name = (node.name or "").strip().lower()
        node_type = (node.node_type or "").strip().lower()
        components = {str(component).strip().lower() for component in node.components}
        text = self._cleanup_hint_text(node.text)

        text_like_tokens = ("text", "label", "richtext", "textfield", "input")
        generic_tokens = {
            "container",
            "component",
            "gcomponent",
            "node",
            "root",
            "canvas",
            "scene",
            "midlayer",
            "mainui",
        }

        if any(token in name for token in text_like_tokens):
            score += 6.0
        if node_type in {"label", "text", "richtext", "richtextfield"}:
            score += 5.0
        if "richtext" in components or "label" in components:
            score += 4.0
        if name in generic_tokens:
            score -= 4.0
        if any(token in name for token in ("msg", "toast", "tips", "notice", "dialog")):
            score += 2.5

        if isinstance(node.pos, list) and len(node.pos) == 2:
            x, y = node.pos
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                # 临时提示通常在中上区域，越接近越优先。
                distance = abs(x - 0.5) + abs(y - 0.24)
                score += max(0.0, 2.5 - distance * 5.0)

        if text:
            score += min(len(text) / 24.0, 2.0)

        return (
            score,
            node.depth,
            len(node.path),
        )

    def _cleanup_hint_text(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        cleaned = re.sub(r"<[^>]+>", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:120]

    def _normalize_hint_text(self, text: str) -> str:
        return re.sub(r"\s+", "", self._cleanup_hint_text(text)).lower()

    def _parse_unlock_text(self, text: str) -> dict[str, str]:
        cleaned = self._cleanup_hint_text(text)
        if not cleaned:
            return {}

        patterns: list[tuple[str, str]] = [
            (r"([^，。]{0,40}\d+\s*级[^，。]{0,40}(?:解锁|开启|开放))", "unlock_requirement_not_met"),
            (r"([^，。]{0,40}等级\s*\d+[^，。]{0,40}(?:解锁|开启|开放))", "unlock_requirement_not_met"),
            (r"(达到\s*\d+\s*级[^，。]{0,20}(?:解锁|开启|开放)?)", "unlock_requirement_not_met"),
            (r"(\d+\s*级(?:后)?(?:解锁|开启|开放))", "unlock_requirement_not_met"),
            (r"(通关[^，。]{0,20}(?:后)?(?:解锁|开启|开放))", "unlock_requirement_not_met"),
            (r"(完成[^，。]{0,20}(?:后)?(?:解锁|开启|开放))", "unlock_requirement_not_met"),
            (r"([^，。]{0,20}(?:暂未开放|尚未开放|未开放))", "feature_not_open"),
            (r"([^，。]{0,20}(?:未开启|尚未开启))", "feature_not_open"),
            (r"([^，。]{0,20}(?:敬请期待))", "feature_not_open"),
            (r"([^，。]{0,20}(?:解锁))", "unlock_requirement_not_met"),
        ]

        for pattern, reason in patterns:
            match = re.search(pattern, cleaned)
            if match:
                hint_text = match.group(1).strip()
                return {
                    "blocked_reason": reason,
                    "unlock_hint_text": cleaned,
                    "unlock_condition": hint_text,
                }
        return {}

    def _log_semantic_event(self, payload: dict[str, Any]) -> None:
        _log_event(self.log_path, payload)

    # ================================================================
    # 返回控制
    # ================================================================

    def _try_go_back(
        self,
        expected_sig: str,
        expected_logical_key: str = "",
        max_attempts: int = 3,
    ) -> bool:
            """尝试返回到期望的页面。"""
            for attempt in range(max_attempts):
                hierarchy = self.connector.dump_hierarchy()
                if hierarchy is None:
                    return False

                current_obs = self.observer.capture(hierarchy)
                if self._same_logical_page(expected_sig, expected_logical_key, current_obs):
                    return True

                # 语义分析当前页面
                page_sem = self.semantic.analyze(current_obs)

                # 【新增逻辑：枢纽节点拦截】
                # 如果当前已经处于主页面（大厅/据点等），禁止用 UI「返回」乱点（易误点退出），也不应再向登录层回退。
                if page_sem.category.value == "lobby":
                    expected_page = self.result.graph.get_page(expected_sig)
                    if expected_page and expected_page.category == "lobby":
                        _log_event(self.log_path, {
                            "kind": "go_back_hub_tolerance",
                            "msg": "已回到枢纽类页面（签名可能与进入子页面前略有差异），视为回退成功",
                            "expected_sig": expected_sig,
                            "expected_logical_page": expected_logical_key,
                            "actual_sig": current_obs.signature,
                            "actual_logical_page": current_obs.logical_page_key,
                        })
                        return True
                    _log_event(self.log_path, {
                        "kind": "go_back_aborted",
                        "msg": "取消回退：当前处于大厅/据点等枢纽页，禁止用 UI 返回键继续向上一层回退",
                        "signature": current_obs.signature,
                        "reason": "cannot_go_back_from_lobby"
                    })
                    return False

                # 尝试点击 back/close 按钮（跳过危险文案，避免把「退出」当返回）
                for sem in page_sem.node_semantics:
                    if sem.role in {ControlRole.BACK, ControlRole.CLOSE}:
                        node_text = f"{sem.node.name or ''} {sem.node.text or ''}".lower()
                        if any(
                            _keyword_matches_node(kw, node_text)
                            for kw in self.config.dangerous_keywords
                        ):
                            continue
                        ok, _ = self.connector.click_node(
                            sem.node.name, sem.node.pos, self._screen_size
                        )
                        if ok:
                            time.sleep(self.config.action_wait_s)
                            after = self.connector.dump_hierarchy()
                            if after:
                                after_obs = self.observer.capture(after)
                                if self._same_logical_page(expected_sig, expected_logical_key, after_obs):
                                    return True
                        break

                # 兜底：Android 返回键
                self.connector.press_back()
                time.sleep(self.config.action_wait_s)

            # 最后检查一次
            hierarchy = self.connector.dump_hierarchy()
            if hierarchy:
                # 【新增】回退后的截图
                screen_path = str(self.screenshot_dir / f"step_{self._step_counter}_back.png")
                self.connector.snapshot(screen_path)
                obs = self.observer.capture(hierarchy, screenshot_path=screen_path) # 【修改】
                return self._same_logical_page(expected_sig, expected_logical_key, obs)
            return False

    # ================================================================
    # 停止条件
    # ================================================================

    def _should_stop(self) -> bool:
        """检查是否应该停止探索。"""
        # 已有明确的停止原因
        if self.result.stop_reason:
            return True

        # 达到最大步数
        if self._step_counter >= self.config.max_steps:
            self.result.stop_reason = "max_steps_reached"
            _log_event(self.log_path, {"kind": "stop", "msg": "达到最大探索步数，准备停止", "reason": "max_steps_reached"})
            return True

        # 达到最大页面数
        if self.result.graph.page_count >= self.config.max_pages:
            self.result.stop_reason = "max_pages_reached"
            _log_event(self.log_path, {"kind": "stop", "msg": "达到最大页面数量限制，准备停止", "reason": "max_pages_reached"})
            return True

        # 连续无新页面
        if self._consecutive_no_new >= self.config.no_new_page_limit:
            self.result.stop_reason = "no_new_pages"
            _log_event(self.log_path, {"kind": "stop", "msg": "连续多次未发现新页面，认为探索完成，准备停止", "reason": "no_new_pages"})
            return True

        return False

    def _looks_like_android_shell_only(self, observation: PageObservation) -> bool:
        """判断当前 UI 树是否只暴露了 Android 原生容器壳。"""
        if self.config.engine_type != ENGINE_ANDROID_UIAUTOMATION:
            return False
        if observation.actionable_candidates or observation.text_nodes:
            return False
        if len(observation.all_nodes) > 12:
            return False

        wrapper_like_count = 0
        for node in observation.all_nodes:
            name = (node.name or "").lower()
            if (
                not name
                or name == "<root>"
                or name.startswith("android.")
                or name.startswith("android:")
                or "layout" in name
                or "android:id/content" in name
            ):
                wrapper_like_count += 1

        return wrapper_like_count >= max(1, len(observation.all_nodes) - 1)

    # ================================================================
    # 输出保存
    # ================================================================

    def _save_outputs(self) -> None:
        """保存所有输出产物。"""
        # 状态图
        self.result.graph.save(self.output_dir / "state_graph.json")

        # Mermaid 图
        mermaid = self.result.graph.to_mermaid()
        (self.output_dir / "state_graph.mmd").write_text(mermaid, encoding="utf-8")

        # 探索执行记录
        executions_data = [e.to_dict() for e in self.result.executions]
        (self.output_dir / "executions.json").write_text(
            json.dumps(executions_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 探索摘要
        (self.output_dir / "exploration_summary.json").write_text(
            json.dumps(self.result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _log_event(self.log_path, {
            "kind": "outputs_saved",
            "msg": f"探索结束：所有结果(截图/日志/状态图)已保存至目录 -> {self.output_dir}", # 【新增】
            "output_dir": str(self.output_dir),
        })

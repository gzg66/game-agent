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
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .action_planner import CandidateAction, ColdStartActionPlanner
from .config import (
    ENGINE_COCOS2DX_JS,
    ENGINE_COCOS2DX_LUA,
    ENGINE_COCOS_CREATOR,
    ENGINE_UNITY3D,
    GameConfig,
)
from .observation import ObservationCapture, PageObservation
from .semantic import ControlRole, PageSemanticInfo, SemanticAnalyzer
from .state_graph import ExplorationGraph


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        self._adb_path: str | None = None

    # ---- 初始化 ----

    def connect(self) -> None:
        """连接设备并初始化 Poco。"""
        from airtest.core.api import connect_device
        connect_device(self.config.device_uri)

        self.poco = self._create_poco()

    def reconnect(self) -> None:
        """重新连接 Poco（用于 RPC 断开后恢复）。"""
        from airtest.core.api import connect_device
        connect_device(self.config.device_uri)
        self.poco = self._create_poco()

    def _create_poco(self) -> Any:
        """根据引擎类型创建对应的 Poco 实例。"""
        engine = self.config.engine_type
        host = self.config.poco_host
        port = self.config.effective_poco_port()

        if engine == ENGINE_UNITY3D:
            from poco.drivers.unity3d import UnityPoco
            return UnityPoco((host, port))

        if engine in {ENGINE_COCOS_CREATOR, ENGINE_COCOS2DX_JS}:
            from poco.drivers.cocosjs import CocosJsPoco
            return CocosJsPoco(addr=(host, port))

        if engine == ENGINE_COCOS2DX_LUA:
            from poco.drivers.std import StdPoco
            return StdPoco(port=port, use_airtest_input=True)

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

    def force_stop_app(self) -> None:
        self.adb_cmd("shell", "am", "force-stop", self.config.package_name)

    def start_app(self) -> None:
        self.adb_cmd("shell", "am", "start", "-n", self.config.activity_name)

    def press_back(self) -> None:
        self.adb_cmd("shell", "input", "keyevent", "4")

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

class ActionExecution:
    """一次动作执行的记录。"""

    def __init__(
        self,
        step: int,
        action: CandidateAction,
        page_before: str,
        page_after: str | None,
        success: bool,
        page_changed: bool,
        click_info: str,
        duration_ms: int,
    ) -> None:
        self.step = step
        self.action = action
        self.page_before = page_before
        self.page_after = page_after
        self.success = success
        self.page_changed = page_changed
        self.click_info = click_info
        self.duration_ms = duration_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action_key": self.action.action_key,
            "action_label": self.action.label,
            "action_role": self.action.role.value,
            "page_before": self.page_before,
            "page_after": self.page_after,
            "success": self.success,
            "page_changed": self.page_changed,
            "click_info": self.click_info,
            "duration_ms": self.duration_ms,
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

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self.connector = GameConnector(config)
        self.observer = ObservationCapture()
        self.semantic = SemanticAnalyzer(config)
        self.planner = ColdStartActionPlanner(config)
        self.result = ColdStartResult()

        # 输出目录
        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / "logs" / "exploration.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            self.log_path.unlink()

        # 运行时状态
        self._screen_size: tuple[int, int] = (1440, 2560)
        self._step_counter: int = 0
        self._consecutive_no_new: int = 0
        self._explored_pages: set[str] = set()

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
            self.result.status = "completed"
            if not self.result.stop_reason:
                self.result.stop_reason = "exploration_completed"

        except Exception as exc:
            self.result.status = "error"
            self.result.stop_reason = f"unexpected_error: {exc}"
            _log_event(self.log_path, {"kind": "error", "error": str(exc)})

        finally:
            self.result.finished_at = _utc_iso()
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

            obs = self.observer.capture(hierarchy)
            _log_event(self.log_path, {
                "kind": "first_screen",
                "msg": f"首屏采集成功：当前所在页面识别为 '{obs.title}'", # 【新增】易读描述
                "signature": obs.signature,
                "title": obs.title,
                "total_nodes": len(obs.all_nodes),
                "clickable_nodes": len(obs.clickable_nodes),
                "text_nodes": len(obs.text_nodes),
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

            # ---- 记录到状态图 ----
            page_node, is_new = self.result.graph.add_page(
                signature=sig,
                title=observation.title,
                category=page_sem.category.value,
                is_popup=page_sem.has_popup,
                is_high_risk=page_sem.has_high_risk,
                action_count=len(observation.clickable_nodes),
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
                "clickable_count": len(observation.clickable_nodes),
                "text_count": len(observation.text_nodes),
            }

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
            })

            # 【修改点 2：新增基于动作集的精细化退出判定】
            # 筛选出当前页面的安全动作（风险等级 < 2）
            safe_candidates = [c for c in candidates if c.risk_level < 2]

            # 如果没有安全动作，或者所有的安全动作都已经探索过了，才判定这个页面不需要继续驻留
            if not safe_candidates:
                return

            all_explored = all(self.planner.is_explored(sig, c.action_key) for c in safe_candidates)
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
                if self.planner.is_explored(sig, action.action_key):
                    continue

                execution = self._execute_action(action, sig)
                if execution is None:
                    continue

                self.result.executions.append(execution)

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
                )

                self.planner.mark_explored(sig, action.action_key)

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

                # 如果页面变化了，递归探索新页面
                if execution.page_changed and execution.page_after:
                    after_hierarchy = self.connector.dump_hierarchy()
                    if after_hierarchy:
                        after_obs = self.observer.capture(after_hierarchy)
                        self._explore_from(
                            after_obs,
                            depth=depth + 1,
                            path_stack=[*path_stack, sig],
                        )

                        # 【修改修复：探索完子页面后，尝试返回并强制校验状态】
                        back_success = self._try_go_back(sig)
                        
                        if not back_success:
                            # 再次通过实时截图确认是否真的回去了
                            current_hierarchy = self.connector.dump_hierarchy()
                            current_sig = self.observer.capture(current_hierarchy).signature if current_hierarchy else ""
                            
                            if current_sig != sig:
                                _log_event(self.log_path, {
                                    "kind": "stranded_aborted",
                                    "msg": "回退失败，当前不在期望页面，终止该页面的剩余动作遍历",
                                    "expected_sig": sig,
                                    "actual_sig": current_sig,
                                })
                                break  # 核心修复：强行跳出当前 candidates 循环，停止瞎点！

    # ================================================================
    # 动作执行
    # ================================================================

    def _execute_action(
        self,
        action: CandidateAction,
        current_sig: str,
    ) -> ActionExecution | None:
        """执行一个候选动作并返回执行记录。"""
        self._step_counter += 1

        _log_event(self.log_path, {
            "kind": "action_start",
            "msg": f"[{self._step_counter}步] 开始执行：点击 '{action.label}' (作用: {action.role.value})", # 【新增】
            "step": self._step_counter,
            "signature": current_sig,
            "action_key": action.action_key,
            "action_label": action.label,
            "action_role": action.role.value,
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
                success=False,
                page_changed=False,
                click_info=click_info,
                duration_ms=duration_ms,
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
                success=False,
                page_changed=False,
                click_info=click_info,
                duration_ms=duration_ms,
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

        after_obs = self.observer.capture(after_hierarchy)
        page_changed = after_obs.signature != current_sig

        _log_event(self.log_path, {
            "kind": "action_end",
            "msg": f"[{self._step_counter}步] 执行完毕：耗时 {duration_ms}ms，页面是否发生跳转: {page_changed}", # 【新增】
            "step": self._step_counter,
            "signature": current_sig,
            "action_key": action.action_key,
            "after_signature": after_obs.signature,
            "page_changed": page_changed,
            "duration_ms": duration_ms,
        })

        return ActionExecution(
            step=self._step_counter,
            action=action,
            page_before=current_sig,
            page_after=after_obs.signature,
            success=True,
            page_changed=page_changed,
            click_info=click_info,
            duration_ms=duration_ms,
        )

    # ================================================================
    # 返回控制
    # ================================================================

    def _try_go_back(self, expected_sig: str, max_attempts: int = 3) -> bool:
            """尝试返回到期望的页面。"""
            for attempt in range(max_attempts):
                hierarchy = self.connector.dump_hierarchy()
                if hierarchy is None:
                    return False

                current_obs = self.observer.capture(hierarchy)
                if current_obs.signature == expected_sig:
                    return True

                # 语义分析当前页面
                page_sem = self.semantic.analyze(current_obs)

                # 【新增逻辑：枢纽节点拦截】
                # 如果当前已经处于主页面（大厅），则禁止尝试通过“返回”或“物理返回键”向上一层（如登录页）回退
                if page_sem.category.value == "lobby":
                    _log_event(self.log_path, {
                        "kind": "go_back_aborted",
                        "msg": "取消回退：当前处于大厅，禁止尝试向上一层回退", # 【新增】
                        "signature": current_obs.signature,
                        "reason": "cannot_go_back_from_lobby"
                    })
                    return False

                # 尝试点击 back/close 按钮
                for sem in page_sem.node_semantics:
                    if sem.role in {ControlRole.BACK, ControlRole.CLOSE}:
                        ok, _ = self.connector.click_node(
                            sem.node.name, sem.node.pos, self._screen_size
                        )
                        if ok:
                            time.sleep(self.config.action_wait_s)
                            after = self.connector.dump_hierarchy()
                            if after:
                                after_obs = self.observer.capture(after)
                                if after_obs.signature == expected_sig:
                                    return True
                        break

                # 兜底：Android 返回键
                self.connector.press_back()
                time.sleep(self.config.action_wait_s)

            # 最后检查一次
            hierarchy = self.connector.dump_hierarchy()
            if hierarchy:
                obs = self.observer.capture(hierarchy)
                return obs.signature == expected_sig
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

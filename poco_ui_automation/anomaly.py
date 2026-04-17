from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    ActionExecution,
    AnomalySignal,
    AnomalyType,
    IssueRecord,
    PageObservation,
    utc_now,
)


@dataclass(slots=True)
class AnomalyDetectorConfig:
    long_stay_threshold_ms: int = 30_000
    no_effect_threshold: int = 3
    max_repeat_state_count: int = 5
    log_error_keywords: list[str] = field(
        default_factory=lambda: ["error", "exception", "assert", "fatal", "crash"]
    )


class AnomalyDetector:
    """运行时异常检测器（MVP）。"""

    def __init__(self, config: AnomalyDetectorConfig | None = None) -> None:
        self.config = config or AnomalyDetectorConfig()
        self._consecutive_no_effect: int = 0
        self._page_dwell_start_ms: float | None = None
        self._current_page_signature: str | None = None
        self._repeat_state_counter: dict[str, int] = {}
        self._seen_issue_keys: set[str] = set()
        self._all_signals: list[AnomalySignal] = []

    @property
    def signals(self) -> list[AnomalySignal]:
        return list(self._all_signals)

    def check_post_action(
        self,
        execution: ActionExecution,
        obs_before: PageObservation,
        obs_after: PageObservation | None,
        step_index: int,
    ) -> list[AnomalySignal]:
        signals: list[AnomalySignal] = []

        if obs_after is None:
            sig = AnomalySignal(
                signal_type=AnomalyType.PROCESS_EXIT,
                source="post_action",
                severity_hint="critical",
                score=1.0,
                page_signature=obs_before.page_signature,
                page_name=obs_before.page_name_raw,
                step_index=step_index,
                evidence={
                    "action_id": execution.action_id,
                    "selector": execution.selector_query,
                },
            )
            signals.append(sig)
            self._all_signals.append(sig)
            return signals

        if not execution.state_changed:
            self._consecutive_no_effect += 1
            if self._consecutive_no_effect >= self.config.no_effect_threshold:
                sig = AnomalySignal(
                    signal_type=AnomalyType.ACTION_NO_EFFECT,
                    source="post_action",
                    severity_hint="medium",
                    score=min(self._consecutive_no_effect / 10.0, 1.0),
                    page_signature=obs_before.page_signature,
                    page_name=obs_before.page_name_raw,
                    step_index=step_index,
                    evidence={
                        "consecutive_count": self._consecutive_no_effect,
                        "last_action": execution.selector_query,
                    },
                )
                signals.append(sig)
                self._all_signals.append(sig)
        else:
            self._consecutive_no_effect = 0

        after_sig = obs_after.page_signature
        self._repeat_state_counter[after_sig] = (
            self._repeat_state_counter.get(after_sig, 0) + 1
        )
        if self._repeat_state_counter[after_sig] >= self.config.max_repeat_state_count:
            sig = AnomalySignal(
                signal_type=AnomalyType.REPEAT_STATE,
                source="post_action",
                severity_hint="medium",
                score=min(self._repeat_state_counter[after_sig] / 10.0, 1.0),
                page_signature=after_sig,
                page_name=obs_after.page_name_raw,
                step_index=step_index,
                evidence={
                    "visit_count": self._repeat_state_counter[after_sig],
                },
            )
            signals.append(sig)
            self._all_signals.append(sig)

        return signals

    def check_dwell_time(
        self,
        current_signature: str,
        elapsed_ms: float,
        step_index: int,
    ) -> list[AnomalySignal]:
        signals: list[AnomalySignal] = []
        if elapsed_ms >= self.config.long_stay_threshold_ms:
            sig = AnomalySignal(
                signal_type=AnomalyType.LONG_STAY,
                source="dwell_check",
                severity_hint="medium",
                score=min(elapsed_ms / 60_000.0, 1.0),
                page_signature=current_signature,
                step_index=step_index,
                evidence={"elapsed_ms": elapsed_ms},
            )
            signals.append(sig)
            self._all_signals.append(sig)
        return signals

    def promote_to_issue(
        self,
        signal: AnomalySignal,
        reproduction_path: list[str],
    ) -> IssueRecord | None:
        dedup_key = f"{signal.signal_type}:{signal.page_signature}"
        if dedup_key in self._seen_issue_keys:
            return None
        self._seen_issue_keys.add(dedup_key)

        category, severity = self._map_signal(signal)
        return IssueRecord(
            category=category,
            severity=severity,
            title=self._build_title(signal),
            page_name=signal.page_name,
            page_signature=signal.page_signature,
            action_index=signal.step_index,
            reproduction_path=reproduction_path,
            details=signal.evidence,
        )

    def reset_page_tracking(self, new_signature: str) -> None:
        self._current_page_signature = new_signature
        self._page_dwell_start_ms = None

    @staticmethod
    def _map_signal(signal: AnomalySignal) -> tuple[str, str]:
        mapping: dict[str, tuple[str, str]] = {
            AnomalyType.PROCESS_EXIT: ("崩溃", "critical"),
            AnomalyType.DISCONNECT: ("崩溃", "critical"),
            AnomalyType.LONG_STAY: ("阻塞", "medium"),
            AnomalyType.REPEAT_STATE: ("阻塞", "medium"),
            AnomalyType.ACTION_NO_EFFECT: ("阻塞", "medium"),
            AnomalyType.LOG_ERROR: ("项目报错", "high"),
            AnomalyType.SUSPICIOUS_NUMBER: ("数值错误", "medium"),
            AnomalyType.MISSING_RESOURCE: ("资源缺失", "medium"),
        }
        return mapping.get(signal.signal_type, ("未知", "low"))

    @staticmethod
    def _build_title(signal: AnomalySignal) -> str:
        titles: dict[str, str] = {
            AnomalyType.PROCESS_EXIT: "游戏进程异常退出",
            AnomalyType.DISCONNECT: "Poco 连接断开",
            AnomalyType.LONG_STAY: "页面长时间停留",
            AnomalyType.REPEAT_STATE: "页面状态重复进入",
            AnomalyType.ACTION_NO_EFFECT: "连续点击无效果",
            AnomalyType.LOG_ERROR: "客户端日志报错",
            AnomalyType.SUSPICIOUS_NUMBER: "疑似数值异常",
            AnomalyType.MISSING_RESOURCE: "疑似资源缺失",
        }
        base = titles.get(signal.signal_type, "异常信号")
        if signal.page_name:
            return f"{base} @ {signal.page_name}"
        return base

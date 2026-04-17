from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
import uuid

from .ai_strategy import StateGraphMemory
from .anomaly import AnomalyDetector, AnomalyDetectorConfig
from .cache import UiStateCache
from .candidate_gen import CandidateGenerator
from .framework import DriverProtocol
from .integration import ProjectProfile
from .models import (
    ActionExecution,
    AnomalySignal,
    CandidateAction,
    ColdStartResult,
    ControlSignal,
    DeviceContext,
    IssueRecord,
    PageObservation,
    PageSnapshot,
    StateTransition,
    utc_now,
)
from .observation import ObservationBuilder
from .persistence import WorldModelStore
from .semantic import SemanticAnalyzer


@dataclass(slots=True)
class ColdStartConfig:
    max_steps: int = 100
    max_pages: int = 30
    max_actions_per_page: int = 8
    consecutive_no_new_page_limit: int = 10
    action_wait_seconds: float = 2.0
    startup_wait_seconds: float = 8.0
    goal: str = ""


class ColdStartExplorer:
    """冷启动探索编排器：6 步流程。"""

    def __init__(
        self,
        profile: ProjectProfile,
        driver: DriverProtocol,
        output_dir: str | Path,
        config: ColdStartConfig | None = None,
    ) -> None:
        self.profile = profile
        self.driver = driver
        self.config = config or ColdStartConfig()
        self.output_dir = Path(output_dir)

        session_id = self._generate_session_id()
        dangerous = set(profile.dangerous_actions)

        self.observer = ObservationBuilder(
            session_id=session_id,
            device_context=DeviceContext(
                device_uri="",
                package_name=profile.package_name,
            ),
        )
        self.semantic = SemanticAnalyzer(
            profile=profile,
            dangerous_keywords=dangerous,
        )
        self.candidate_gen = CandidateGenerator(
            dangerous_keywords=dangerous,
            max_candidates_per_page=self.config.max_actions_per_page,
        )
        self.memory = StateGraphMemory()
        self.anomaly_detector = AnomalyDetector()
        self.store = WorldModelStore(self.output_dir)
        self.cache = UiStateCache()

        self._session_id = session_id
        self._step_index: int = 0
        self._issues: list[IssueRecord] = []
        self._action_history: list[ActionExecution] = []
        self._started_at = utc_now()

    def run(self) -> ColdStartResult:
        self.store.init_dirs()

        observation = self._observe()
        if observation is None:
            return self._build_result("initial_observation_failed")

        page_state, semantic_nodes = self.semantic.analyze(observation)
        self.memory.remember_semantic_page(page_state)
        self.memory.remember_node(self._to_page_snapshot(observation))
        self.store.append_observation(observation)

        while not self._should_stop():
            self._step_index += 1

            candidates = self.candidate_gen.generate(
                page_signature=observation.page_signature,
                semantic_nodes=semantic_nodes,
                memory=self.memory,
                goal=self.config.goal,
            )

            if not candidates:
                control = self._try_backtrack()
                if control == ControlSignal.STOP:
                    break
                observation = self._observe()
                if observation is None:
                    break
                page_state, semantic_nodes = self.semantic.analyze(observation)
                self.memory.remember_semantic_page(page_state)
                self.memory.remember_node(self._to_page_snapshot(observation))
                self.store.append_observation(observation)
                continue

            chosen = candidates[0]
            execution, obs_after = self._execute_action(chosen, observation)
            self._action_history.append(execution)
            self.store.append_action(execution)

            signals = self.anomaly_detector.check_post_action(
                execution, observation, obs_after, self._step_index
            )
            for signal in signals:
                issue = self.anomaly_detector.promote_to_issue(
                    signal, self._build_reproduction_path()
                )
                if issue:
                    self._issues.append(issue)

            if obs_after is None:
                break

            if execution.state_changed:
                transition = StateTransition(
                    transition_id=f"t_{self._step_index}",
                    from_signature=execution.page_signature_before,
                    to_signature=execution.page_signature_after or "",
                    action_id=chosen.action_id,
                    action_key=chosen.selector_query,
                    semantic_intent=chosen.semantic_intent,
                    success=execution.success,
                    avg_duration_ms=float(execution.duration_ms),
                )
                self.memory.remember_transition(transition)
                self.memory.remember_edge(
                    execution.page_signature_before,
                    execution.page_signature_after or "",
                    "click",
                    chosen.selector_query,
                    execution.duration_ms,
                    execution.success,
                )

            observation = obs_after
            page_state, semantic_nodes = self.semantic.analyze(observation)
            self.memory.remember_semantic_page(page_state)
            self.memory.remember_node(self._to_page_snapshot(observation))
            self.store.append_observation(observation)

            if self._step_index % 5 == 0:
                self._persist_state()

        self._persist_state()
        result = self._build_result(self._determine_stop_reason())
        self.store.save_cold_start_result(result)
        return result

    def _observe(self) -> PageObservation | None:
        try:
            return self.observer.observe(self.driver, self._step_index)
        except Exception:
            return None

    def _execute_action(
        self,
        candidate: CandidateAction,
        obs_before: PageObservation,
    ) -> tuple[ActionExecution, PageObservation | None]:
        start = time.perf_counter()
        success = False
        try:
            success = self.driver.click(candidate.selector_query)
        except Exception:
            pass
        duration_ms = int((time.perf_counter() - start) * 1000)

        if self.config.action_wait_seconds > 0:
            time.sleep(self.config.action_wait_seconds)

        obs_after = self._observe()
        sig_after = obs_after.page_signature if obs_after else None
        state_changed = sig_after is not None and sig_after != obs_before.page_signature

        execution = ActionExecution(
            execution_id=f"exec_{self._step_index}",
            session_id=self._session_id,
            step_index=self._step_index,
            action_id=candidate.action_id,
            selector_query=candidate.selector_query,
            semantic_intent=candidate.semantic_intent,
            page_signature_before=obs_before.page_signature,
            page_signature_after=sig_after,
            page_name_before=obs_before.page_name_raw,
            page_name_after=obs_after.page_name_raw if obs_after else "",
            success=success,
            state_changed=state_changed,
            duration_ms=duration_ms,
        )
        return execution, obs_after

    def _try_backtrack(self) -> ControlSignal:
        try:
            ok = self.driver.back()
            if not ok:
                return ControlSignal.STOP
        except Exception:
            return ControlSignal.STOP
        if self.config.action_wait_seconds > 0:
            time.sleep(self.config.action_wait_seconds)
        return ControlSignal.CONTINUE

    def _should_stop(self) -> bool:
        if self._step_index >= self.config.max_steps:
            return True
        if len(self.memory.semantic_pages) >= self.config.max_pages:
            return True
        if (
            self._step_index > 0
            and self.memory.consecutive_no_new_pages() >= self.config.consecutive_no_new_page_limit
        ):
            return True
        if any(issue.severity == "critical" for issue in self._issues):
            return True
        return False

    def _persist_state(self) -> None:
        self.store.save_semantic_pages(self.memory.semantic_pages)
        self.store.save_transitions(self.memory.transitions)
        self.store.save_issues(self._issues)

    def _build_result(self, stop_reason: str) -> ColdStartResult:
        pages = self.memory.semantic_pages
        role_counter: dict[str, int] = {}
        for page in pages.values():
            for label in page.key_clickables:
                role_counter[label] = role_counter.get(label, 0) + 1

        module_entries: list[str] = []
        risk_areas: list[str] = []
        for page in pages.values():
            if page.page_type not in ("unknown", "dialog"):
                entry = f"{page.page_type}:{page.canonical_page_name}"
                if entry not in module_entries:
                    module_entries.append(entry)
            if page.risk_flags:
                risk_areas.append(
                    f"{page.canonical_page_name}({','.join(page.risk_flags)})"
                )

        high_value_paths: list[list[str]] = []
        for t in self.memory.transitions.values():
            if t.success:
                from_page = pages.get(t.from_signature)
                to_page = pages.get(t.to_signature)
                if from_page and to_page:
                    high_value_paths.append(
                        [from_page.canonical_page_name, t.action_key, to_page.canonical_page_name]
                    )

        return ColdStartResult(
            session_id=self._session_id,
            project_name=self.profile.project_name,
            new_pages_count=len(pages),
            merged_pages_count=sum(1 for p in pages.values() if p.seen_count > 1),
            module_entries=module_entries,
            widget_semantic_tags=role_counter,
            high_value_paths=high_value_paths[:20],
            risk_areas=risk_areas,
            anomalies=self.anomaly_detector.signals,
            issues=list(self._issues),
            total_steps=self._step_index,
            total_actions=len(self._action_history),
            stop_reason=stop_reason,
            started_at=self._started_at,
            finished_at=utc_now(),
        )

    def _build_reproduction_path(self) -> list[str]:
        return [
            f"{a.semantic_intent}:{a.selector_query}" for a in self._action_history[-6:]
        ]

    def _determine_stop_reason(self) -> str:
        if any(issue.severity == "critical" for issue in self._issues):
            return "critical_issue"
        if self._step_index >= self.config.max_steps:
            return "max_steps_reached"
        if len(self.memory.semantic_pages) >= self.config.max_pages:
            return "max_pages_reached"
        if self.memory.consecutive_no_new_pages() >= self.config.consecutive_no_new_page_limit:
            return "no_new_pages"
        return "completed"

    @staticmethod
    def _to_page_snapshot(observation: PageObservation) -> PageSnapshot:
        return PageSnapshot(
            signature=observation.page_signature,
            page_name=observation.page_name_raw,
            nodes=observation.ui_tree,
            captured_at=observation.captured_at,
            key_texts=[n.text for n in observation.text_nodes if n.text][:8],
            root_names=[n.name for n in observation.root_nodes if n.name],
            metadata=observation.metadata,
        )

    @staticmethod
    def _generate_session_id() -> str:
        return f"cs_{uuid.uuid4().hex[:8]}"

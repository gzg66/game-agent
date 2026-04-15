"""集成缓存与同步视觉分析的增强版语义分析器。"""

from __future__ import annotations

import time
from typing import Any

from .observation import PageObservation
from .semantic import ControlRole, NodeSemanticInfo, PageCategory, PageSemanticInfo, SemanticAnalyzer
from .semantic_cache import SemanticCache
from .som_vision import SoMVisionService


class EnhancedSemanticAnalyzer(SemanticAnalyzer):
    def __init__(self, config: Any, llm_client: Any = None, event_callback: Any = None):
        super().__init__(config)
        self.cache = SemanticCache(cache_dir=f"{config.output_dir}/semantic_cache")
        self.vision_service = SoMVisionService(llm_client)
        self._event_callback = event_callback
        self._stats: dict[str, int | float] = {
            "pages_analyzed": 0,
            "cache_hit_pages": 0,
            "cache_miss_pages": 0,
            "cache_write_count": 0,
            "vision_sync_pages": 0,
            "vision_completed_pages": 0,
            "vision_candidate_nodes": 0,
            "vision_enriched_nodes": 0,
            "vision_calls_saved_by_cache": 0,
            "total_vision_latency_ms": 0.0,
            "llm_submitted_pages": 0,
            "llm_completed_pages": 0,
            "llm_candidate_nodes": 0,
            "llm_enriched_nodes": 0,
            "llm_calls_saved_by_cache": 0,
            "total_llm_latency_ms": 0.0,
            "vision_llm_skipped_pages": 0,
        }

    def analyze(self, observation: PageObservation) -> PageSemanticInfo:
        page_sig = observation.signature
        self._increment_stat("pages_analyzed")

        cached_data = self.cache.get(page_sig)
        vision_mode = getattr(self.config, "vision_mode", "rule_first")
        if (
            cached_data
            and vision_mode == "vision_first"
            and cached_data.get("semantic_source") not in {"vision", "rule_degraded"}
        ):
            cached_data = None

        if cached_data:
            self._increment_stat("cache_hit_pages")
            self._increment_stat("vision_calls_saved_by_cache")
            self._increment_stat("llm_calls_saved_by_cache")
            self._emit_event({
                "kind": "semantic_cache_hit",
                "signature": page_sig,
                "page_title": observation.title,
                "cache_key": page_sig,
                "semantic_source": cached_data.get("semantic_source", "cache"),
                "cached_node_count": len(cached_data.get("node_semantics", [])),
                "llm_enriched_node_count": cached_data.get("llm_enriched_node_count", 0),
                "saved_at": cached_data.get("saved_at", ""),
            })
            return self._build_semantic_from_cache(observation, cached_data)

        self._increment_stat("cache_miss_pages")
        self._emit_event({
            "kind": "semantic_cache_miss",
            "signature": page_sig,
            "page_title": observation.title,
            "semantic_source": "rule_then_vision",
            "reason": "not_found",
        })

        page_semantic = super().analyze(observation)
        page_semantic.semantic_source = "rule"
        page_semantic.cache_hit = False
        page_semantic.actionable_candidate_count = len(observation.actionable_candidates)
        page_semantic.blocked_action_count = sum(
            1
            for node_info in page_semantic.node_semantics
            if node_info.blocked_reason or node_info.unlock_hint_text
        )
        page_semantic.llm_enriched_node_count = 0
        page_semantic.llm_pending = False

        vision_candidates = self._select_vision_candidates(page_semantic.node_semantics)
        page_semantic.llm_candidate_count = len(vision_candidates)
        unknown_count = sum(1 for n in page_semantic.node_semantics if n.role == ControlRole.UNKNOWN)
        rule_labeled_count = len(page_semantic.node_semantics) - unknown_count

        self._emit_event({
            "kind": "semantic_fast_path_done",
            "signature": page_sig,
            "page_title": observation.title,
            "rule_labeled_count": rule_labeled_count,
            "unknown_count": unknown_count,
            "weak_primary_entry_count": 0,
            "llm_candidate_count": len(vision_candidates),
            "actionable_candidate_count": page_semantic.actionable_candidate_count,
        })

        llm_available = bool(self.vision_service.llm_client and observation.screenshot_path)
        skip_llm = False
        if vision_mode == "vision_first":
            skip_llm = self._should_skip_vision_llm(observation, page_semantic)
            if skip_llm:
                self._increment_stat("vision_llm_skipped_pages")
                self._emit_event({
                    "kind": "vision_llm_skipped",
                    "signature": page_sig,
                    "page_title": observation.title,
                    "category": page_semantic.category.value,
                    "category_confidence": page_semantic.category_confidence,
                    "unknown_count": unknown_count,
                    "reason": "simple_page_rule_sufficient",
                })
            elif llm_available and vision_candidates:
                self._run_sync_vision(observation, page_semantic, vision_candidates)
            else:
                page_semantic.degraded_mode = True
                page_semantic.semantic_source = "rule_degraded"
                self._emit_event({
                    "kind": "vision_degraded",
                    "signature": page_sig,
                    "page_title": observation.title,
                    "reason": "missing_llm_or_candidates",
                    "llm_available": llm_available,
                    "candidate_count": len(vision_candidates),
                })

        self.cache.put(page_sig, page_semantic)
        self._increment_stat("cache_write_count")
        if page_semantic.llm_enriched_node_count:
            write_reason = "vision_enriched"
        elif skip_llm:
            write_reason = "rule_llm_skipped"
        else:
            write_reason = "base_rule_only"
        self._emit_event({
            "kind": "semantic_cache_written",
            "signature": page_sig,
            "page_title": observation.title,
            "node_semantic_count": len(page_semantic.node_semantics),
            "llm_updated_count": page_semantic.llm_enriched_node_count,
            "write_reason": write_reason,
        })
        return page_semantic

    def _should_skip_vision_llm(
        self,
        observation: PageObservation,
        page_semantic: PageSemanticInfo,
    ) -> bool:
        """vision_first 下是否跳过 SoM/LLM：登录等简单页用规则即可。"""
        cats = getattr(self.config, "vision_skip_llm_for_categories", None) or []
        min_conf = float(getattr(self.config, "vision_skip_llm_min_page_category_confidence", 0.25))
        cat_val = page_semantic.category.value
        if cats and cat_val in cats and page_semantic.category_confidence >= min_conf:
            return True
        markers = getattr(self.config, "vision_skip_llm_text_markers_any", None) or []
        if not markers:
            return False
        blob = self._observation_text_blob(observation)
        return any(m.strip() and m.lower() in blob for m in markers if m)

    @staticmethod
    def _observation_text_blob(observation: PageObservation) -> str:
        parts: list[str] = [observation.title or ""]
        for node in observation.all_nodes:
            if node.text:
                parts.append(node.text)
        return " ".join(parts).lower()

    def _run_sync_vision(
        self,
        observation: PageObservation,
        page_semantic: PageSemanticInfo,
        vision_candidates: list[NodeSemanticInfo],
    ) -> None:
        self._increment_stat("vision_sync_pages")
        self._increment_stat("vision_candidate_nodes", len(vision_candidates))
        self._increment_stat("llm_submitted_pages")
        self._increment_stat("llm_candidate_nodes", len(vision_candidates))
        self._emit_event({
            "kind": "som_llm_submitted",
            "signature": observation.signature,
            "page_title": observation.title,
            "screenshot_path": observation.screenshot_path,
            "candidate_count": len(vision_candidates),
            "candidate_node_paths": [n.node.path for n in vision_candidates],
            "mode": "sync_vision_first",
        })

        start = time.perf_counter()
        vision_results = self.vision_service.analyze_candidates(
            observation.screenshot_path,
            vision_candidates,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        updated_count = self._apply_vision_results(page_semantic.node_semantics, vision_results)
        page_semantic.llm_enriched_node_count = updated_count
        page_semantic.semantic_source = "vision" if updated_count else "rule_degraded"
        page_semantic.degraded_mode = updated_count == 0

        self._increment_stat("vision_completed_pages")
        self._increment_stat("vision_enriched_nodes", updated_count)
        self._increment_stat("total_vision_latency_ms", latency_ms)
        self._increment_stat("llm_completed_pages")
        self._increment_stat("llm_enriched_nodes", updated_count)
        self._increment_stat("total_llm_latency_ms", latency_ms)
        self._emit_event({
            "kind": "som_llm_completed",
            "signature": observation.signature,
            "page_title": observation.title,
            "latency_ms": latency_ms,
            "updated_count": updated_count,
            "updated_node_paths": sorted(vision_results.keys()),
            "mode": "sync_vision_first",
        })

    def _select_vision_candidates(self, node_semantics: list[NodeSemanticInfo]) -> list[NodeSemanticInfo]:
        max_candidates = max(1, int(getattr(self.config, "vision_max_candidates", 16)))
        filtered = [
            node_info
            for node_info in node_semantics
            if self._has_valid_vision_anchor(node_info)
        ]
        filtered.sort(
            key=lambda info: (
                info.node.candidate_score,
                info.confidence,
                1 if (info.node.text or "").strip() else 0,
                1 if info.node.clickable else 0,
                -info.node.depth,
            ),
            reverse=True,
        )
        return filtered[:max_candidates]

    def _has_valid_vision_anchor(self, node_info: NodeSemanticInfo) -> bool:
        pos = node_info.node.pos
        size = node_info.node.size
        if not isinstance(pos, list) or len(pos) != 2:
            return False
        if not isinstance(size, list) or len(size) != 2:
            return False
        x, y = pos
        w, h = size
        return (
            isinstance(x, (int, float))
            and isinstance(y, (int, float))
            and isinstance(w, (int, float))
            and isinstance(h, (int, float))
            and 0.0 <= x <= 1.0
            and 0.0 <= y <= 1.0
            and w > 0
            and h > 0
        )

    def _apply_vision_results(
        self,
        node_semantics: list[NodeSemanticInfo],
        vision_results: dict[str, dict[str, Any]],
    ) -> int:
        updated_count = 0
        for node_info in node_semantics:
            result = vision_results.get(node_info.node.path)
            if not result:
                continue

            updated_count += 1
            node_info.semantic_source = "vision"
            node_info.actionability_reason = result.get("reason", "vision_inferred")
            node_info.is_actionable = bool(result.get("is_actionable", node_info.is_actionable))

            raw_confidence = result.get("confidence", node_info.confidence)
            if isinstance(raw_confidence, (int, float)):
                node_info.confidence = max(node_info.confidence, float(raw_confidence))

            role = self._safe_role(result.get("action_type"))
            if role is not None:
                node_info.role = role
                node_info.role_reason = result.get("reason", "vision_inferred")

            if node_info.role == ControlRole.DANGEROUS_ACTION:
                node_info.risk_level = 2

            node_info.priority_score = self._compute_priority(
                node_info.role,
                node_info.node,
                node_info.risk_level,
                node_info.confidence,
            )
        return updated_count

    def _safe_role(self, raw_role: Any) -> ControlRole | None:
        if not isinstance(raw_role, str):
            return None
        normalized = raw_role.strip().upper()
        try:
            return ControlRole[normalized]
        except KeyError:
            return None

    def shutdown(self, wait: bool = True) -> None:
        del wait

    def get_stats(self) -> dict[str, int | float]:
        stats = dict(self._stats)
        pages_analyzed = int(stats.get("pages_analyzed", 0))
        stats["cache_hit_rate"] = (
            round(float(stats["cache_hit_pages"]) / pages_analyzed, 4)
            if pages_analyzed else 0.0
        )
        vision_completed_pages = int(stats.get("vision_completed_pages", 0))
        stats["avg_vision_latency_ms"] = (
            round(float(stats["total_vision_latency_ms"]) / vision_completed_pages, 2)
            if vision_completed_pages else 0.0
        )
        llm_completed_pages = int(stats.get("llm_completed_pages", 0))
        stats["avg_llm_latency_ms"] = (
            round(float(stats["total_llm_latency_ms"]) / llm_completed_pages, 2)
            if llm_completed_pages else 0.0
        )
        return stats

    def _build_semantic_from_cache(self, obs: PageObservation, cached_data: dict) -> PageSemanticInfo:
        path_to_cached = {n["node_path"]: n for n in cached_data["node_semantics"]}
        node_semantics: list[NodeSemanticInfo] = []
        for node in obs.actionable_candidates:
            cached_node = path_to_cached.get(node.path)
            if cached_node:
                node_semantics.append(NodeSemanticInfo(
                    node=node,
                    role=ControlRole(cached_node["role"]),
                    risk_level=cached_node["risk_level"],
                    role_reason=cached_node["role_reason"] + " (Cached)",
                    priority_score=float(cached_node.get("priority_score", 0.0)),
                    confidence=float(cached_node.get("confidence", 0.0)),
                    semantic_source=str(cached_node.get("semantic_source", "cache")),
                    is_actionable=bool(cached_node.get("is_actionable", True)),
                    actionability_reason=str(cached_node.get("actionability_reason", "cached")),
                    blocked_reason=str(cached_node.get("blocked_reason", "")),
                    unlock_hint_text=str(cached_node.get("unlock_hint_text", "")),
                    unlock_condition=str(cached_node.get("unlock_condition", "")),
                ))
            else:
                node_semantics.append(self._classify_node(node))

        return PageSemanticInfo(
            observation=obs,
            category=PageCategory(cached_data["category"]),
            category_confidence=1.0,
            category_reason=cached_data["category_reason"] + " (Cached)",
            has_popup=cached_data["has_popup"],
            has_high_risk=cached_data["has_high_risk"],
            node_semantics=node_semantics,
            semantic_source=str(cached_data.get("semantic_source", "cache")),
            cache_hit=True,
            llm_candidate_count=0,
            llm_enriched_node_count=cached_data.get("llm_enriched_node_count", 0),
            llm_pending=False,
            actionable_candidate_count=int(cached_data.get("actionable_candidate_count", len(obs.actionable_candidates))),
            blocked_action_count=int(
                cached_data.get(
                    "blocked_action_count",
                    sum(
                        1
                        for node_info in node_semantics
                        if node_info.blocked_reason or node_info.unlock_hint_text
                    ),
                )
            ),
            degraded_mode=bool(cached_data.get("degraded_mode", False)),
        )

    def _emit_event(self, payload: dict[str, Any]) -> None:
        if self._event_callback:
            self._event_callback(payload)

    def _increment_stat(self, key: str, amount: int | float = 1) -> None:
        current = self._stats.get(key, 0)
        self._stats[key] = current + amount
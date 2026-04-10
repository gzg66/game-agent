"""集成缓存与异步 LLM 调用的增强版语义分析器。"""
import concurrent.futures
import time
import threading
from typing import Any
from .semantic import SemanticAnalyzer, PageSemanticInfo, ControlRole, NodeSemanticInfo, PageCategory
from .observation import PageObservation
from .semantic_cache import SemanticCache
from .som_vision import SoMVisionService

class EnhancedSemanticAnalyzer(SemanticAnalyzer):
    def __init__(self, config: Any, llm_client: Any = None, event_callback: Any = None):
        super().__init__(config)
        self.cache = SemanticCache(cache_dir=f"{config.output_dir}/semantic_cache")
        self.vision_service = SoMVisionService(llm_client)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()
        self._pending_futures: set[concurrent.futures.Future[Any]] = set()
        self._event_callback = event_callback
        self._stats: dict[str, int | float] = {
            "pages_analyzed": 0,
            "cache_hit_pages": 0,
            "cache_miss_pages": 0,
            "cache_write_count": 0,
            "llm_submitted_pages": 0,
            "llm_completed_pages": 0,
            "llm_candidate_nodes": 0,
            "llm_enriched_nodes": 0,
            "llm_calls_saved_by_cache": 0,
            "total_llm_latency_ms": 0.0,
        }

    def analyze(self, observation: PageObservation) -> PageSemanticInfo:
        page_sig = observation.signature
        self._increment_stat("pages_analyzed")

        # 1. 尝试读缓存
        cached_data = self.cache.get(page_sig)
        if cached_data:
            self._increment_stat("cache_hit_pages")
            self._increment_stat("llm_calls_saved_by_cache")
            self._emit_event({
                "kind": "semantic_cache_hit",
                "signature": page_sig,
                "page_title": observation.title,
                "cache_key": page_sig,
                "semantic_source": "cache",
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
            "semantic_source": "rule_then_llm",
            "reason": "not_found",
        })

        # 2. 跑本地规则快车道
        base_semantic = super().analyze(observation)
        base_semantic.semantic_source = "rule"
        base_semantic.cache_hit = False

        # 3. 选出适合交给 SoM 视觉复判的节点：
        #    - 所有规则无法识别的 unknown 节点
        #    - 无文本图片类节点中，被弱规则误判为 primary_entry 的节点
        llm_candidates = [
            n for n in base_semantic.node_semantics
            if self._should_use_llm_for_node(n)
        ]
        unknown_count = sum(1 for n in base_semantic.node_semantics if n.role == ControlRole.UNKNOWN)
        weak_primary_entry_count = sum(
            1
            for n in base_semantic.node_semantics
            if n.role == ControlRole.PRIMARY_ENTRY and self._should_use_llm_for_node(n)
        )
        base_semantic.llm_candidate_count = len(llm_candidates)
        base_semantic.llm_enriched_node_count = 0
        base_semantic.llm_pending = bool(
            llm_candidates and observation.screenshot_path and self.vision_service.llm_client
        )
        rule_labeled_count = len(base_semantic.node_semantics) - unknown_count
        self._emit_event({
            "kind": "semantic_fast_path_done",
            "signature": page_sig,
            "page_title": observation.title,
            "rule_labeled_count": rule_labeled_count,
            "unknown_count": unknown_count,
            "weak_primary_entry_count": weak_primary_entry_count,
            "llm_candidate_count": len(llm_candidates),
        })

        # 4. 触发异步视觉分析任务
        if llm_candidates and observation.screenshot_path and self.vision_service.llm_client:
            print(
                f"[视觉大模型] 已提交 SoM 异步分析: page={observation.title}, "
                f"candidates={len(llm_candidates)}, screenshot={observation.screenshot_path}"
            )
            self._increment_stat("llm_submitted_pages")
            self._increment_stat("llm_candidate_nodes", len(llm_candidates))
            self._emit_event({
                "kind": "som_llm_submitted",
                "signature": page_sig,
                "page_title": observation.title,
                "screenshot_path": observation.screenshot_path,
                "candidate_count": len(llm_candidates),
                "candidate_node_paths": [n.node.path for n in llm_candidates],
            })
            future = self.executor.submit(
                self._async_llm_task,
                observation,
                base_semantic,
                llm_candidates,
            )
            self._pending_futures.add(future)
            future.add_done_callback(self._pending_futures.discard)
        else:
            self.cache.put(page_sig, base_semantic)
            self._increment_stat("cache_write_count")
            self._emit_event({
                "kind": "semantic_cache_written",
                "signature": page_sig,
                "page_title": observation.title,
                "node_semantic_count": len(base_semantic.node_semantics),
                "llm_updated_count": 0,
                "write_reason": "base_rule_only",
            })

        return base_semantic

    def _should_use_llm_for_node(self, node_info: NodeSemanticInfo) -> bool:
        if node_info.role == ControlRole.UNKNOWN:
            return True

        node = node_info.node
        node_name = (node.name or "").lower()
        role_reason = node_info.role_reason.lower()
        no_text = not (node.text or "").strip()
        image_like = node.node_type == "Image" or node_name in {"image", "img"}
        weak_primary_entry = (
            node_info.role == ControlRole.PRIMARY_ENTRY
            and no_text
            and image_like
            and any(token in role_reason for token in ["play", "enter", "go", "start"])
        )
        return weak_primary_entry

    def _async_llm_task(
        self,
        observation: PageObservation,
        base_semantic: PageSemanticInfo,
        llm_candidates: list[NodeSemanticInfo],
    ) -> None:
        start = time.perf_counter()
        try:
            print(
                f"[视觉大模型] 开始 SoM 分析: page={observation.title}, "
                f"candidates={len(llm_candidates)}"
            )
            role_mapping = self.vision_service.analyze_unknown_nodes(
                observation.screenshot_path,
                llm_candidates,
            )
            updated_count = 0
            updated_node_paths: list[str] = []
            if role_mapping:
                with self._lock:
                    for n_info in base_semantic.node_semantics:
                        if n_info.node.path in role_mapping:
                            inferred_role_str = role_mapping[n_info.node.path].upper()
                            try:
                                n_info.role = ControlRole[inferred_role_str]
                                n_info.role_reason = "LLM_SoM_Inferred"
                                updated_count += 1
                                updated_node_paths.append(n_info.node.path)
                            except KeyError:
                                pass

            base_semantic.llm_enriched_node_count = updated_count
            base_semantic.llm_pending = False
            self.cache.put(observation.signature, base_semantic)

            latency_ms = int((time.perf_counter() - start) * 1000)
            self._increment_stat("llm_completed_pages")
            self._increment_stat("llm_enriched_nodes", updated_count)
            self._increment_stat("cache_write_count")
            self._increment_stat("total_llm_latency_ms", latency_ms)
            self._emit_event({
                "kind": "som_llm_completed",
                "signature": observation.signature,
                "page_title": observation.title,
                "latency_ms": latency_ms,
                "updated_count": updated_count,
                "updated_node_paths": updated_node_paths,
            })
            self._emit_event({
                "kind": "semantic_cache_written",
                "signature": observation.signature,
                "page_title": observation.title,
                "node_semantic_count": len(base_semantic.node_semantics),
                "llm_updated_count": updated_count,
                "write_reason": "llm_enriched" if updated_count else "base_rule_only",
            })
            print(
                f"[视觉大模型] SoM 分析完成: page={observation.title}, updated={updated_count}"
            )
        except Exception as exc:
            base_semantic.llm_pending = False
            self.cache.put(observation.signature, base_semantic)
            self._increment_stat("cache_write_count")
            self._emit_event({
                "kind": "som_llm_failed",
                "signature": observation.signature,
                "page_title": observation.title,
                "error": str(exc),
            })
            self._emit_event({
                "kind": "semantic_cache_written",
                "signature": observation.signature,
                "page_title": observation.title,
                "node_semantic_count": len(base_semantic.node_semantics),
                "llm_updated_count": 0,
                "write_reason": "base_rule_only",
            })
            print(f"[视觉大模型] SoM 未返回有效结果: page={observation.title}")

    def shutdown(self, wait: bool = True) -> None:
        if wait and self._pending_futures:
            print(f"[视觉大模型] 等待 {len(self._pending_futures)} 个异步任务完成...")
        self.executor.shutdown(wait=wait)

    def get_stats(self) -> dict[str, int | float]:
        with self._lock:
            stats = dict(self._stats)
        pages_analyzed = int(stats.get("pages_analyzed", 0))
        stats["cache_hit_rate"] = (
            round(float(stats["cache_hit_pages"]) / pages_analyzed, 4)
            if pages_analyzed else 0.0
        )
        llm_completed_pages = int(stats.get("llm_completed_pages", 0))
        stats["avg_llm_latency_ms"] = (
            round(float(stats["total_llm_latency_ms"]) / llm_completed_pages, 2)
            if llm_completed_pages else 0.0
        )
        return stats

    def _build_semantic_from_cache(self, obs: PageObservation, cached_data: dict) -> PageSemanticInfo:
        path_to_cached = {n["node_path"]: n for n in cached_data["node_semantics"]}
        node_semantics = []
        for node in obs.clickable_nodes:
            if node.path in path_to_cached:
                c = path_to_cached[node.path]
                node_semantics.append(NodeSemanticInfo(
                    node=node,
                    role=ControlRole(c["role"]),
                    risk_level=c["risk_level"],
                    role_reason=c["role_reason"] + " (Cached)"
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
            semantic_source="cache",
            cache_hit=True,
            llm_candidate_count=0,
            llm_enriched_node_count=cached_data.get("llm_enriched_node_count", 0),
            llm_pending=False,
        )

    def _emit_event(self, payload: dict[str, Any]) -> None:
        if self._event_callback:
            self._event_callback(payload)

    def _increment_stat(self, key: str, amount: int | float = 1) -> None:
        with self._lock:
            current = self._stats.get(key, 0)
            self._stats[key] = current + amount
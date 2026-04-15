"""基于页面签名的本地语义缓存层。"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_CACHE_VERSION = 4

class SemanticCache:
    def __init__(self, cache_dir: str = "outputs/cold_start/semantic_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "semantic_cache.json"
        self._cache_data: dict[str, Any] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self._cache_data = json.load(f)
            except Exception as e:
                print(f"加载语义缓存失败: {e}")

    def _save_cache(self) -> None:
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存语义缓存失败: {e}")

    def get(self, page_signature: str) -> Optional[dict[str, Any]]:
        cached = self._cache_data.get(page_signature)
        if not cached:
            return None
        if cached.get("cache_version") != _CACHE_VERSION:
            return None
        return cached

    def put(self, page_signature: str, semantic_info: Any) -> None:
        serialized_nodes = []
        for n_info in semantic_info.node_semantics:
            serialized_nodes.append({
                "node_path": n_info.node.path,
                "role": n_info.role.value,
                "risk_level": n_info.risk_level,
                "role_reason": n_info.role_reason,
                "priority_score": n_info.priority_score,
                "confidence": getattr(n_info, "confidence", 0.0),
                "semantic_source": getattr(n_info, "semantic_source", "rule"),
                "is_actionable": getattr(n_info, "is_actionable", True),
                "actionability_reason": getattr(n_info, "actionability_reason", ""),
                "blocked_reason": getattr(n_info, "blocked_reason", ""),
                "unlock_hint_text": getattr(n_info, "unlock_hint_text", ""),
                "unlock_condition": getattr(n_info, "unlock_condition", ""),
            })

        llm_enriched_node_count = sum(
            1
            for n_info in semantic_info.node_semantics
            if "LLM_SoM_Inferred" in n_info.role_reason
        )

        self._cache_data[page_signature] = {
            "cache_version": _CACHE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "category": semantic_info.category.value,
            "category_reason": semantic_info.category_reason,
            "has_popup": semantic_info.has_popup,
            "has_high_risk": semantic_info.has_high_risk,
            "semantic_source": getattr(semantic_info, "semantic_source", "rule"),
            "llm_enriched_node_count": llm_enriched_node_count,
            "actionable_candidate_count": getattr(semantic_info, "actionable_candidate_count", 0),
            "blocked_action_count": getattr(
                semantic_info,
                "blocked_action_count",
                sum(
                    1
                    for n_info in semantic_info.node_semantics
                    if getattr(n_info, "blocked_reason", "")
                    or getattr(n_info, "unlock_hint_text", "")
                ),
            ),
            "degraded_mode": getattr(semantic_info, "degraded_mode", False),
            "node_semantics": serialized_nodes
        }
        self._save_cache()
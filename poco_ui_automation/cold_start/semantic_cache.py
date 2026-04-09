"""基于页面签名的本地语义缓存层。"""
import json
import os
from pathlib import Path
from typing import Any, Optional

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
        return self._cache_data.get(page_signature)

    def put(self, page_signature: str, semantic_info: Any) -> None:
        serialized_nodes = []
        for n_info in semantic_info.node_semantics:
            serialized_nodes.append({
                "node_path": n_info.node.path,
                "role": n_info.role.value,
                "risk_level": n_info.risk_level,
                "role_reason": n_info.role_reason
            })

        self._cache_data[page_signature] = {
            "category": semantic_info.category.value,
            "category_reason": semantic_info.category_reason,
            "has_popup": semantic_info.has_popup,
            "has_high_risk": semantic_info.has_high_risk,
            "node_semantics": serialized_nodes
        }
        self._save_cache()
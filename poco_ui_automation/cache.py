from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .models import PageSnapshot, PageState, Selector, SelectorStats, utc_now


class RefreshReason:
    INITIAL = "initial"
    PAGE_CHANGED = "page_changed"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_NOT_VISIBLE = "target_not_visible"
    ASSERTION_FAILED = "assertion_failed"
    AI_REPLAN = "ai_replan"
    TTL_EXPIRED = "ttl_expired"


@dataclass(slots=True)
class CachePolicy:
    snapshot_ttl_seconds: int = 3
    stable_window_seconds: int = 2


class UiStateCache:
    """缓存页面快照、稳定窗口和成功 selector。"""

    def __init__(self, policy: CachePolicy | None = None) -> None:
        self.policy = policy or CachePolicy()
        self._pages: dict[str, PageState] = {}
        self._current_signature: str | None = None

    def get(self, signature: str) -> PageState | None:
        return self._pages.get(signature)

    def current(self) -> PageState | None:
        if not self._current_signature:
            return None
        return self._pages.get(self._current_signature)

    def upsert_snapshot(
        self,
        snapshot: PageSnapshot,
        reason: str = RefreshReason.INITIAL,
    ) -> PageState:
        stable_until = utc_now() + timedelta(seconds=self.policy.stable_window_seconds)
        existing = self._pages.get(snapshot.signature)
        if existing:
            existing.snapshot = snapshot
            existing.stable_until = stable_until
            existing.last_refresh_reason = reason
            state = existing
        else:
            state = PageState(
                snapshot=snapshot,
                stable_until=stable_until,
                last_refresh_reason=reason,
            )
            self._pages[snapshot.signature] = state
        self._current_signature = snapshot.signature
        return state

    def should_refresh(self, state: PageState | None) -> bool:
        if not state:
            return True
        age = utc_now() - state.snapshot.captured_at
        return age.total_seconds() > self.policy.snapshot_ttl_seconds

    def needs_stable_refresh(self, state: PageState | None) -> bool:
        if not state or not state.stable_until:
            return True
        return utc_now() > state.stable_until

    def remember_selector(self, signature: str, key: str, selector: Selector) -> None:
        state = self._pages.get(signature)
        if not state:
            raise KeyError(f"页面签名不存在: {signature}")
        stats = state.cached_selectors.get(key)
        if not stats:
            state.cached_selectors[key] = SelectorStats(selector=selector)
            return
        stats.selector = selector

    def mark_selector_result(self, signature: str, key: str, success: bool) -> None:
        state = self._pages.get(signature)
        if not state:
            return
        stats = state.cached_selectors.get(key)
        if not stats:
            return
        if success:
            stats.success_count += 1
        else:
            stats.fail_count += 1
        stats.last_used_at = utc_now()

    def get_cached_selector(self, signature: str, key: str) -> Selector | None:
        state = self._pages.get(signature)
        if not state:
            return None
        selector_stats = state.cached_selectors.get(key)
        if not selector_stats:
            return None
        return selector_stats.selector

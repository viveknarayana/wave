"""
KV-aware routing and simple per-worker pressure tracking.

This does not touch vLLM's internal KV cache directly. Instead, it keeps a
rough proxy measure per worker based on:

- active_conversations
- approx_context_tokens (running average)

and uses that to pick a worker for NEW conversations, while preserving
affinity for existing conversations (conversation_id -> worker_id stays
stable in Redis).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from gateway.worker_client import list_worker_targets


@dataclass
class WorkerKVStats:
    active_conversations: int = 0
    avg_context_tokens: float = 0.0
    # Simple notion of KV "capacity": when pressure >= capacity, worker is saturated.
    kv_capacity: float = 1_000_000.0

    @property
    def pressure(self) -> float:
        return self.active_conversations * max(self.avg_context_tokens, 1.0)


class KVAwareRouter:
    def __init__(
        self,
        worker_ids: List[str],
        max_pressure_budget: float = 1_000_000.0,
        saturation_threshold: float = 0.8,
    ) -> None:
        self._stats: Dict[str, WorkerKVStats] = {wid: WorkerKVStats(kv_capacity=max_pressure_budget) for wid in worker_ids}
        self._max_pressure_budget = max_pressure_budget
        self._saturation_threshold = saturation_threshold
        # Track a rough set of conversation_ids per worker for potential eviction.
        self._worker_conversations: Dict[str, List[str]] = {wid: [] for wid in worker_ids}

    def record_conversation(
        self,
        worker_id: str,
        context_tokens: int,
        is_new: bool,
        conversation_id: Optional[str] = None,
    ) -> None:
        """
        Update stats when we route a conversation to a worker.

        - If is_new: increment active_conversations.
        - Always: update running avg_context_tokens (simple EMA-like update).
        """
        if worker_id not in self._stats:
            self._stats[worker_id] = WorkerKVStats(kv_capacity=self._max_pressure_budget)
            self._worker_conversations[worker_id] = []
        s = self._stats[worker_id]
        if is_new:
            s.active_conversations += 1
            if conversation_id:
                self._worker_conversations[worker_id].append(conversation_id)
        # Simple running average / EMA blend
        alpha = 0.2
        s.avg_context_tokens = (1 - alpha) * s.avg_context_tokens + alpha * max(context_tokens, 1)

    def choose_worker_for_new_conversation(self, approx_tokens: int) -> str:
        """
        Pick the worker with lowest KV pressure for a new conversation.
        If all workers are above the budget, still pick the least-loaded one;
        higher layers (admission control) can decide to reject if needed.
        """
        if not self._stats:
            # Fallback: single default worker id
            return os.environ.get("DEFAULT_WORKER_ID", "worker-1")

        # Sort by pressure ascending
        best_id, _ = min(self._stats.items(), key=lambda item: item[1].pressure)
        return best_id

    # --- Saturation + eviction helpers ---

    def is_saturated(self, worker_id: str) -> bool:
        """Return True if worker is past its KV saturation threshold."""
        stats = self._stats.get(worker_id)
        if not stats:
            return False
        return stats.pressure >= stats.kv_capacity * self._saturation_threshold

    def all_saturated(self) -> bool:
        """Return True if all known workers are saturated."""
        if not self._stats:
            return False
        return all(self.is_saturated(wid) for wid in self._stats)

    def evict_one_conversation(self, worker_id: str) -> Optional[str]:
        """
        Evict a single conversation from the given worker, if any.

        Policy: FIFO over the rough list of conversation_ids we recorded when
        they were first seen. Decrements active_conversations and returns the
        evicted conversation_id so callers can drop external affinity state
        (e.g. Redis mapping) if desired.
        """
        stats = self._stats.get(worker_id)
        if not stats:
            return None
        convs = self._worker_conversations.get(worker_id)
        if not convs:
            return None
        evicted = convs.pop(0)
        if stats.active_conversations > 0:
            stats.active_conversations -= 1
        return evicted

    def evict_specific_conversation(self, worker_id: str, conversation_id: str) -> bool:
        """
        Evict a specific conversation from a worker's tracking set (if present).
        Returns True if it was removed.
        """
        if not worker_id or not conversation_id:
            return False
        stats = self._stats.get(worker_id)
        convs = self._worker_conversations.get(worker_id)
        if not stats or not convs:
            return False
        try:
            convs.remove(conversation_id)
        except ValueError:
            return False
        if stats.active_conversations > 0:
            stats.active_conversations -= 1
        return True


def default_kv_router() -> KVAwareRouter:
    """
    Build a router from env/worker ids.

    - If WORKER_BASE_URL is set, assume single worker "worker-1".
    - Else, WORKER_1_URL, WORKER_2_URL, ... (sorted worker-1, worker-2, ...).
    """
    targets = list_worker_targets()
    if targets:
        return KVAwareRouter([wid for wid, _ in targets])
    return KVAwareRouter([os.environ.get("DEFAULT_WORKER_ID", "worker-1")])


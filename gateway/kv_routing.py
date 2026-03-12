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
from typing import Dict, List


@dataclass
class WorkerKVStats:
    active_conversations: int = 0
    avg_context_tokens: float = 0.0

    @property
    def pressure(self) -> float:
        return self.active_conversations * max(self.avg_context_tokens, 1.0)


class KVAwareRouter:
    def __init__(self, worker_ids: List[str], max_pressure_budget: float = 1_000_000.0) -> None:
        self._stats: Dict[str, WorkerKVStats] = {wid: WorkerKVStats() for wid in worker_ids}
        self._max_pressure_budget = max_pressure_budget

    def record_conversation(self, worker_id: str, context_tokens: int, is_new: bool) -> None:
        """
        Update stats when we route a conversation to a worker.

        - If is_new: increment active_conversations.
        - Always: update running avg_context_tokens (simple EMA-like update).
        """
        if worker_id not in self._stats:
            self._stats[worker_id] = WorkerKVStats()
        s = self._stats[worker_id]
        if is_new:
            s.active_conversations += 1
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


def default_kv_router() -> KVAwareRouter:
    """
    Build a router from env/worker ids.

    - If WORKER_BASE_URL is set, assume single worker "worker-1".
    - Else, try WORKER_1_URL, WORKER_2_URL, ... (mirrors worker_client).
    """
    worker_ids: List[str] = []
    if os.environ.get("WORKER_BASE_URL"):
        worker_ids = ["worker-1"]
    else:
        for k in os.environ:
            if k.startswith("WORKER_") and k.endswith("_URL") and k != "WORKER_BASE_URL":
                wid = k.replace("WORKER_", "").replace("_URL", "").lower()
                wid = f"worker-{wid}" if wid.isdigit() else wid
                worker_ids.append(wid)
        if not worker_ids:
            worker_ids = ["worker-1"]
    return KVAwareRouter(worker_ids)


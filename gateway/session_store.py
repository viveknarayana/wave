"""Redis-backed session store: conversation_id -> worker_id for affinity routing."""
from __future__ import annotations

import os
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL_SECONDS = 3600  # 1 hour

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def get_worker_for_conversation(conversation_id: str) -> str | None:
    """Return worker_id if this conversation is pinned to a worker, else None."""
    if not conversation_id:
        return None
    try:
        return _get_client().get(f"conv:{conversation_id}")
    except redis.RedisError:
        return None


def set_worker_for_conversation(conversation_id: str, worker_id: str) -> None:
    """Pin conversation to worker (for affinity routing)."""
    if not conversation_id or not worker_id:
        return
    try:
        _get_client().setex(
            f"conv:{conversation_id}",
            SESSION_TTL_SECONDS,
            worker_id,
        )
    except redis.RedisError:
        pass  # non-fatal for Phase 1; metrics can track failures later


def clear_worker_for_conversation(conversation_id: str) -> None:
    """Remove affinity mapping so future requests can be re-routed."""
    if not conversation_id:
        return
    try:
        _get_client().delete(f"conv:{conversation_id}")
    except redis.RedisError:
        pass

"""
Conversation-scoped prompt cache: exact match + optional semantic (embedding) match.
Cache key space is (conversation_id, model). No cross-conversation reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.environ.get("PROMPT_CACHE_TTL", "3600"))  # 1 hour
ENABLE_PROMPT_CACHE = os.environ.get("ENABLE_PROMPT_CACHE", "1") == "1"
SEMANTIC_CACHE_THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.92"))
SEMANTIC_CONTEXT_MESSAGES = int(os.environ.get("SEMANTIC_CONTEXT_MESSAGES", "2"))  # last N messages for context
MAX_SEMANTIC_ENTRIES_PER_CONV = int(os.environ.get("MAX_SEMANTIC_ENTRIES_PER_CONV", "100"))

_cache_client: redis.Redis | None = None
_embedder: Any = None


def _get_client() -> redis.Redis:
    global _cache_client
    if _cache_client is None:
        _cache_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _cache_client


def _get_embedder():
    """Lazy-load sentence-transformers; if not installed, semantic cache is disabled."""
    global _embedder
    if _embedder is not None:
        return _embedder
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        return _embedder
    except Exception:
        _embedder = False  # type: ignore[assignment]
        return _embedder


def _normalize_prompt(text: str) -> str:
    """Normalize for exact cache key: strip, collapse whitespace, lowercase."""
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def _build_prompt_for_exact(messages: list[dict]) -> str:
    """Single string from messages for exact cache (e.g. last user message or full)."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _build_prompt_with_context(messages: list[dict], last_n: int = 2) -> str:
    """Last N messages + current prompt for embedding (disambiguate 'What is it?' by context)."""
    if last_n <= 0:
        return _build_prompt_for_exact(messages)
    take = messages[-max(1, last_n * 2) :]  # rough: last_n exchanges (user+assistant pairs) + current
    return _build_prompt_for_exact(take)


def _exact_key(conversation_id: str, model: str, normalized_prompt: str) -> str:
    h = hashlib.sha256(f"{conversation_id}:{model}:{normalized_prompt}".encode()).hexdigest()
    return f"cache:exact:{h}"


def _semantic_key(conversation_id: str, model: str) -> str:
    return f"cache:semantic:{conversation_id}:{model}"


def get_cached(
    conversation_id: str,
    model: str,
    messages: list[dict],
) -> tuple[Optional[dict], Optional[str]]:
    """
    Check exact then semantic cache. Returns (response_dict, cache_hit_type) or (None, None).
    response_dict is the raw OpenAI-style completion response to return.
    """
    if not ENABLE_PROMPT_CACHE or not conversation_id:
        return None, None

    client = _get_client()
    prompt_str = _build_prompt_for_exact(messages)
    normalized = _normalize_prompt(prompt_str)
    if not normalized:
        return None, None

    try:
        # 1) Exact
        key = _exact_key(conversation_id, model, normalized)
        raw = client.get(key)
        if raw:
            data = json.loads(raw)
            return data, "exact"

        # 2) Semantic (if embedder available)
        emb = _get_embedder()
        if emb is False:
            return None, None

        prompt_with_ctx = _build_prompt_with_context(messages, SEMANTIC_CONTEXT_MESSAGES)
        q_vec = emb.encode(prompt_with_ctx, normalize_embeddings=True)
        q_vec_list = q_vec.tolist()

        skey = _semantic_key(conversation_id, model)
        entries_json = client.get(skey)
        if not entries_json:
            return None, None

        entries = json.loads(entries_json)
        best_score = -1.0
        best_response = None

        for entry in entries:
            stored_vec = entry.get("embedding") or []
            if not stored_vec:
                continue
            # Cosine similarity (vectors assumed normalized)
            dot = sum(a * b for a, b in zip(q_vec_list, stored_vec))
            if dot > best_score:
                best_score = dot
                best_response = entry.get("response")

        if best_response is not None and best_score >= SEMANTIC_CACHE_THRESHOLD:
            return best_response, "semantic"

        return None, None

    except (redis.RedisError, json.JSONDecodeError):
        return None, None


def set_cached(
    conversation_id: str,
    model: str,
    messages: list[dict],
    response_dict: dict,
) -> None:
    """Store response in exact and (if embedder available) semantic cache."""
    if not ENABLE_PROMPT_CACHE or not conversation_id:
        return

    client = _get_client()
    prompt_str = _build_prompt_for_exact(messages)
    normalized = _normalize_prompt(prompt_str)
    if not normalized:
        return

    try:
        # Exact
        key = _exact_key(conversation_id, model, normalized)
        client.setex(key, CACHE_TTL, json.dumps(response_dict))

        # Semantic
        emb = _get_embedder()
        if emb is False:
            return

        prompt_with_ctx = _build_prompt_with_context(messages, SEMANTIC_CONTEXT_MESSAGES)
        vec = emb.encode(prompt_with_ctx, normalize_embeddings=True)
        skey = _semantic_key(conversation_id, model)

        entry = {"embedding": vec.tolist(), "response": response_dict}

        # Append and trim to max entries (FIFO)
        existing = client.get(skey)
        entries = json.loads(existing) if existing else []
        entries.append(entry)
        if len(entries) > MAX_SEMANTIC_ENTRIES_PER_CONV:
            entries = entries[-MAX_SEMANTIC_ENTRIES_PER_CONV:]
        client.setex(skey, CACHE_TTL, json.dumps(entries))

    except (redis.RedisError, TypeError):
        pass

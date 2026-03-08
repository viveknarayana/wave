"""
Phase 2: Call OpenAI-compatible worker (e.g. vLLM) at /v1/chat/completions.
Worker URL from env: WORKER_BASE_URL or WORKER_1_URL, WORKER_2_URL, ...
"""

import os
from typing import Any, Dict, Optional

import httpx

# Single worker: WORKER_BASE_URL=http://localhost:8000
# Multi-worker: WORKER_1_URL=http://localhost:8000, WORKER_2_URL=http://localhost:8001
WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "")
WORKER_URL_MAP: Dict[str, str] = {}
for k, v in os.environ.items():
    if k.startswith("WORKER_") and k.endswith("_URL") and k != "WORKER_BASE_URL":
        # WORKER_1_URL -> worker-1
        worker_id = k.replace("WORKER_", "").replace("_URL", "").lower()
        worker_id = f"worker-{worker_id}" if worker_id.isdigit() else worker_id
        WORKER_URL_MAP[worker_id] = v.rstrip("/")


def get_worker_url(worker_id: str) -> Optional[str]:
    """Resolve worker_id to base URL. Returns None if no worker configured."""
    if WORKER_BASE_URL:
        return WORKER_BASE_URL
    return WORKER_URL_MAP.get(worker_id)


async def call_worker(
    base_url: str,
    body: Dict[str, Any],
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """
    POST to worker's /v1/chat/completions. Body is OpenAI-style (model, messages, ...).
    Returns the JSON response dict. Raises httpx.HTTPError on failure.
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()


async def stream_worker(
    base_url: str,
    body: Dict[str, Any],
    timeout: float = 60.0,
):
    """
    POST to worker with stream=True; yields SSE bytes (chunked).
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk


async def get_worker_health(base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    GET worker's /health. Returns {"status": "ok", "worker": base_url} or raises.
    """
    url = f"{base_url.rstrip('/')}/health"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return {"worker": base_url, **data}

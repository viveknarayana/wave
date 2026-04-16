"""
Wave gateway: OpenAI-compatible /v1/chat/completions with tenant config,
Redis session store (conversation_id -> worker_id), and Prometheus metrics.
"""

import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from gateway.config import get_tenant_config
from gateway.metrics import (
    ADMISSION_REJECTIONS,
    CACHE_HITS,
    CACHE_MISSES,
    INFLIGHT_REQUESTS,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    REQUEST_LATENCY_MS_BY_TIER,
    REQUESTS_BY_TIER,
    ERROR_COUNT,
    QUEUE_DEPTH,
    get_metrics_bytes,
    get_metrics_content_type,
)
from gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatChoice,
    ChatChoiceMessage,
    Usage,
    WaveUsage,
)
from gateway.session_store import (
    get_worker_for_conversation,
    set_worker_for_conversation,
    clear_worker_for_conversation,
)
from gateway.worker_client import (
    get_worker_url,
    call_worker,
    stream_worker,
    get_worker_health,
    list_worker_targets,
)
from gateway.batching import PriorityBatcher, BatchRequest
from gateway.kv_routing import default_kv_router
from gateway.prompt_cache import get_cached, set_cached


DEFAULT_WORKER_ID = "worker-1"
ENABLE_PRIORITY_BATCHING = os.environ.get("ENABLE_PRIORITY_BATCHING", "1") == "1"
SLO_WINDOW_SECONDS = int(os.environ.get("SLO_WINDOW_SECONDS", "120"))
SLO_EVAL_INTERVAL_SECONDS = int(os.environ.get("SLO_EVAL_INTERVAL_SECONDS", "15"))

TIER_SLOS = {
    "premium": {"p95_latency_ms": 1000, "error_rate": 0.001},
    "standard": {"p95_latency_ms": 3000, "error_rate": 0.01},
}

batcher: PriorityBatcher | None = None
kv_router = default_kv_router()
_slo_events: dict[str, deque[tuple[float, int, bool]]] = {
    "premium": deque(),
    "standard": deque(),
}
_slo_last_eval_ts: float = 0.0
_slo_violation_state: dict[str, bool] = {"premium": False, "standard": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global batcher  # noqa: PLW0603

    if ENABLE_PRIORITY_BATCHING:
        batcher = PriorityBatcher(call_worker)
        await batcher.start()
    try:
        yield
    finally:
        if batcher is not None:
            await batcher.stop()


app = FastAPI(title="Wave Gateway", version="0.1.0", lifespan=lifespan)


def _tenant_tier(raw_tenant_id: str | None) -> str:
    if raw_tenant_id == "premium":
        return "premium"
    # For now, anything non-premium maps to standard SLO policy.
    return "standard"


def _prune_slo_window(now_ts: float) -> None:
    for tier in _slo_events:
        window = _slo_events[tier]
        while window and (now_ts - window[0][0]) > SLO_WINDOW_SECONDS:
            window.popleft()


def _p95_ms_from_events(window: deque[tuple[float, int, bool]]) -> float:
    if not window:
        return 0.0
    latencies = sorted(event[1] for event in window)
    idx = max(0, int(0.95 * len(latencies)) - 1)
    return float(latencies[idx])


def _evaluate_slo_violations(now_ts: float) -> None:
    global _slo_last_eval_ts  # noqa: PLW0603
    if (now_ts - _slo_last_eval_ts) < SLO_EVAL_INTERVAL_SECONDS:
        return
    _slo_last_eval_ts = now_ts
    _prune_slo_window(now_ts)
    for tier, target in TIER_SLOS.items():
        window = _slo_events[tier]
        if not window:
            _slo_violation_state[tier] = False
            continue
        p95_ms = _p95_ms_from_events(window)
        errors = sum(1 for _, _, ok in window if not ok)
        error_rate = errors / len(window)
        _slo_violation_state[tier] = (
            p95_ms > target["p95_latency_ms"] or error_rate > target["error_rate"]
        )


def _record_tier_outcome(tenant_tier: str, status_code: int, latency_ms: int) -> None:
    status = "ok" if 200 <= status_code < 400 else "error"
    REQUESTS_BY_TIER.labels(tenant_tier=tenant_tier, status=status).inc()
    REQUEST_LATENCY_MS_BY_TIER.labels(tenant_tier=tenant_tier, status=status).observe(latency_ms)
    _slo_events[tenant_tier].append((time.time(), latency_ms, status == "ok"))


def _should_reject_for_slo(tenant_id: str | None) -> bool:
    # Reject/deprioritize free traffic first when any SLO tier is currently violated.
    return (tenant_id or "free") == "free" and any(_slo_violation_state.values())


def _validate_request(req: ChatCompletionRequest) -> None:
    """Tenant config lookup and request validation."""
    tenant = get_tenant_config(req.tenant_id)
    allowed = tenant.get("models", ["all"])
    if "all" not in allowed and req.model not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Model {req.model} not allowed for tenant {req.tenant_id or 'default'}",
        )
    max_context = tenant.get("max_context", 4096)
    total_content_len = sum(len(m.content) for m in req.messages)
    # Rough: ~4 chars per token
    if (total_content_len // 4) > max_context:
        raise HTTPException(
            status_code=400,
            detail=f"Context length exceeds tenant limit ({max_context} tokens)",
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> ChatCompletionResponse:
    """OpenAI-compatible chat completions with tenant validation and session store."""
    path = "/v1/chat/completions"
    start = time.perf_counter()
    status = "200"
    tenant_tier = _tenant_tier(body.tenant_id)
    now_ts = time.time()
    _evaluate_slo_violations(now_ts)
    if _should_reject_for_slo(body.tenant_id):
        ADMISSION_REJECTIONS.labels(tenant_tier=tenant_tier, reason="slo_violation").inc()
        REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
        REQUEST_COUNT.labels(method="POST", path=path, status="429").inc()
        ERROR_COUNT.labels(path=path, reason="admission_rejected").inc()
        _record_tier_outcome(tenant_tier, 429, int((time.perf_counter() - start) * 1000))
        raise HTTPException(
            status_code=429,
            detail="SLO protection active; retry shortly or use higher-priority tier.",
            headers={"Retry-After": "5"},
        )

    try:
        _validate_request(body)
    except HTTPException as e:
        REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
        REQUEST_COUNT.labels(method="POST", path=path, status=str(e.status_code)).inc()
        ERROR_COUNT.labels(path=path, reason="validation").inc()
        _record_tier_outcome(tenant_tier, int(e.status_code), int((time.perf_counter() - start) * 1000))
        raise

    # Approximate context length (tokens) for KV pressure proxy.
    approx_tokens = sum(len(m.content) // 4 for m in body.messages)
    tenant_id = body.tenant_id or "free"
    if batcher is not None:
        QUEUE_DEPTH.set(float(batcher.queue_depth))
        INFLIGHT_REQUESTS.set(float(batcher.inflight_requests))
    else:
        QUEUE_DEPTH.set(0.0)
        INFLIGHT_REQUESTS.set(0.0)

    # Affinity + KV-aware routing:
    # - Existing conversation_id -> stick to its worker (affinity).
    # - New conversation -> choose worker with lowest KV pressure.
    worker_id = None
    is_new_conversation = False
    if body.conversation_id:
        worker_id = get_worker_for_conversation(body.conversation_id)
        if not worker_id:
            worker_id = kv_router.choose_worker_for_new_conversation(approx_tokens)
            set_worker_for_conversation(body.conversation_id, worker_id)
            is_new_conversation = True
    if not worker_id:
        # No conversation_id: treat this as a short one-off; use KV router anyway.
        worker_id = kv_router.choose_worker_for_new_conversation(approx_tokens)

    # If this conversation is pinned to a saturated worker, unpin + reroute (cold start).
    if body.conversation_id and not is_new_conversation and worker_id and kv_router.is_saturated(worker_id):
        if tenant_id != "premium":
            kv_router.evict_specific_conversation(worker_id, body.conversation_id)
            clear_worker_for_conversation(body.conversation_id)
            worker_id = kv_router.choose_worker_for_new_conversation(approx_tokens)
            set_worker_for_conversation(body.conversation_id, worker_id)
            is_new_conversation = True

    # KV admission control: if all workers are saturated, shed load.
    if kv_router.all_saturated():
        # Evict / reject free-tier traffic first: reject free when saturated.
        if tenant_id != "premium":
            # Try to free up capacity by evicting one conversation from the chosen worker.
            evicted = kv_router.evict_one_conversation(worker_id)
            if evicted:
                clear_worker_for_conversation(evicted)
                worker_id = kv_router.choose_worker_for_new_conversation(approx_tokens)
            else:
                REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
                REQUEST_COUNT.labels(method="POST", path=path, status="503").inc()
                ERROR_COUNT.labels(path=path, reason="kv_saturated").inc()
                _record_tier_outcome(tenant_tier, 503, int((time.perf_counter() - start) * 1000))
                raise HTTPException(
                    status_code=503,
                    detail="KV capacity saturated; please retry later or upgrade tier.",
                )
        else:
            # Premium is rejected when the entire fleet is saturated.
            REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
            REQUEST_COUNT.labels(method="POST", path=path, status="503").inc()
            ERROR_COUNT.labels(path=path, reason="kv_saturated").inc()
            _record_tier_outcome(tenant_tier, 503, int((time.perf_counter() - start) * 1000))
            raise HTTPException(
                status_code=503,
                detail="KV capacity saturated (premium); please retry shortly.",
            )

    kv_router.record_conversation(
        worker_id,
        approx_tokens,
        is_new_conversation,
        conversation_id=body.conversation_id,
    )

    worker_url = get_worker_url(worker_id)
    if worker_url:
        try:
            worker_body = {
                "model": body.model,
                "messages": [{"role": m.role, "content": m.content} for m in body.messages],
                "stream": body.stream,
                "max_tokens": body.max_tokens,
                "temperature": body.temperature,
            }
            if body.stream:
                REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
                REQUEST_COUNT.labels(method="POST", path=path, status=status).inc()
                _record_tier_outcome(tenant_tier, 200, int((time.perf_counter() - start) * 1000))
                return StreamingResponse(
                    stream_worker(worker_url, worker_body),
                    media_type="text/event-stream",
                )

            # Non-streaming: try conversation-scoped prompt cache first, then worker.
            if not body.stream and body.conversation_id:
                messages_for_cache = [{"role": m.role, "content": m.content} for m in body.messages]
                cached, cache_hit_type = get_cached(body.conversation_id, body.model, messages_for_cache)
                if cached is not None and cache_hit_type:
                    CACHE_HITS.labels(type=cache_hit_type).inc()
                    choices = cached.get("choices", [])
                    usage_raw = cached.get("usage") or {}
                    msg = choices[0].get("message", {}) if choices else {}
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    response = ChatCompletionResponse(
                        id=cached.get("id", f"chatcmpl-{uuid.uuid4().hex[:24]}"),
                        model=cached.get("model", body.model),
                        choices=[
                            ChatChoice(
                                message=ChatChoiceMessage(
                                    role=msg.get("role", "assistant"),
                                    content=msg.get("content", ""),
                                ),
                                finish_reason=choices[0].get("finish_reason", "stop") if choices else "stop",
                            )
                        ],
                        usage=Usage(
                            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
                            total_tokens=int(usage_raw.get("prompt_tokens", 0)) + int(usage_raw.get("completion_tokens", 0)),
                        ),
                        wave=WaveUsage(
                            latency_ms=latency_ms,
                            tokens_in=int(usage_raw.get("prompt_tokens", 0)),
                            tokens_out=int(usage_raw.get("completion_tokens", 0)),
                            model_version=cached.get("model", body.model),
                            cost_estimate=(int(usage_raw.get("prompt_tokens", 0)) + int(usage_raw.get("completion_tokens", 0))) * 0.000_001,
                            cache_hit=cache_hit_type,
                        ),
                    )
                    REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
                    REQUEST_COUNT.labels(method="POST", path=path, status=status).inc()
                    _record_tier_outcome(tenant_tier, 200, int((time.perf_counter() - start) * 1000))
                    return response
                CACHE_MISSES.inc()

            # Non-streaming: optionally go through priority-aware batcher.
            if ENABLE_PRIORITY_BATCHING and batcher is not None:
                # Treat premium tenant as high priority; others as free/standard.
                tenant_id = body.tenant_id or "free"
                priority = "premium" if tenant_id == "premium" else "free"
                raw = await batcher.enqueue(
                    BatchRequest(
                        worker_url=worker_url,
                        body=worker_body,
                        priority=priority,
                    )
                )
            else:
                raw = await call_worker(worker_url, worker_body)
            latency_ms = int((time.perf_counter() - start) * 1000)
            choices = raw.get("choices", [])
            usage_raw = raw.get("usage") or {}
            tokens_in = int(usage_raw.get("prompt_tokens", 0))
            tokens_out = int(usage_raw.get("completion_tokens", 0))
            msg = choices[0].get("message", {}) if choices else {}
            response = ChatCompletionResponse(
                id=raw.get("id", f"chatcmpl-{uuid.uuid4().hex[:24]}"),
                model=raw.get("model", body.model),
                choices=[
                    ChatChoice(
                        message=ChatChoiceMessage(
                            role=msg.get("role", "assistant"),
                            content=msg.get("content", ""),
                        ),
                        finish_reason=choices[0].get("finish_reason", "stop") if choices else "stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=tokens_in,
                    completion_tokens=tokens_out,
                    total_tokens=tokens_in + tokens_out,
                ),
                wave=WaveUsage(
                    latency_ms=latency_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    model_version=raw.get("model", body.model),
                    cost_estimate=(tokens_in + tokens_out) * 0.000_001,
                    cache_hit=None,
                ),
            )
            # Store in conversation-scoped prompt cache for future hits.
            if body.conversation_id:
                set_cached(
                    body.conversation_id,
                    body.model,
                    [{"role": m.role, "content": m.content} for m in body.messages],
                    raw,
                )
            REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
            REQUEST_COUNT.labels(method="POST", path=path, status=status).inc()
            _record_tier_outcome(tenant_tier, 200, int((time.perf_counter() - start) * 1000))
            return response
        except Exception:
            ERROR_COUNT.labels(path=path, reason="worker_error").inc()
            _record_tier_outcome(tenant_tier, 502, int((time.perf_counter() - start) * 1000))
            # Fall through to stub

    # No worker configured or worker failed: stub response
    latency_ms = int((time.perf_counter() - start) * 1000)
    tokens_in = sum(len(m.content) // 4 for m in body.messages)
    tokens_out = 10
    model_version = f"{body.model}-v1"
    cost_estimate = (tokens_in + tokens_out) * 0.000_001
    response = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        model=body.model,
        choices=[
            ChatChoice(
                message=ChatChoiceMessage(
                    role="assistant",
                    content="[Wave stub: no worker configured or worker error. Set WORKER_BASE_URL.]",
                ),
                finish_reason="stop",
            )
        ],
        usage=Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out, total_tokens=tokens_in + tokens_out),
        wave=WaveUsage(
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_version=model_version,
            cost_estimate=cost_estimate,
        ),
    )

    REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="POST", path=path, status=status).inc()
    _record_tier_outcome(tenant_tier, 200, int((time.perf_counter() - start) * 1000))
    return response


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(
        content=get_metrics_bytes(),
        media_type=get_metrics_content_type(),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/worker/health")
async def worker_health() -> dict:
    """Probe each configured worker's /health (single WORKER_BASE_URL or WORKER_1_URL, ...)."""
    targets = list_worker_targets()
    if not targets:
        return {"status": "no_worker", "workers": []}
    workers: list[dict] = []
    any_ok = False
    for wid, base in targets:
        try:
            data = await get_worker_health(base)
            any_ok = True
            workers.append({"worker_id": wid, "status": "ok", **data})
        except Exception as e:  # noqa: BLE001
            workers.append(
                {
                    "worker_id": wid,
                    "status": "error",
                    "worker": base,
                    "detail": str(e),
                }
            )
    if not any_ok:
        raise HTTPException(status_code=503, detail={"workers": workers})
    return {
        "status": "ok" if all(w.get("status") == "ok" for w in workers) else "degraded",
        "workers": workers,
    }


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> Response:
    """Count 5xx and re-raise so FastAPI returns 500."""
    path = getattr(request.url, "path", "/v1/chat/completions")
    ERROR_COUNT.labels(path=path, reason="unhandled").inc()
    REQUEST_COUNT.labels(method=request.method, path=path, status="500").inc()
    raise exc

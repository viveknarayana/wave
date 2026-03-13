"""
Wave gateway: OpenAI-compatible /v1/chat/completions with tenant config,
Redis session store (conversation_id -> worker_id), and Prometheus metrics.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from gateway.config import get_tenant_config
from gateway.metrics import (
    REQUEST_COUNT,
    REQUEST_LATENCY,
    ERROR_COUNT,
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
from gateway.session_store import get_worker_for_conversation, set_worker_for_conversation
from gateway.worker_client import get_worker_url, call_worker, stream_worker, get_worker_health
from gateway.batching import PriorityBatcher, BatchRequest
from gateway.kv_routing import default_kv_router
from gateway.prompt_cache import get_cached, set_cached


DEFAULT_WORKER_ID = "worker-1"
ENABLE_PRIORITY_BATCHING = os.environ.get("ENABLE_PRIORITY_BATCHING", "1") == "1"

batcher: PriorityBatcher | None = None
kv_router = default_kv_router()


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

    try:
        _validate_request(body)
    except HTTPException as e:
        REQUEST_LATENCY.labels(path=path).observe(time.perf_counter() - start)
        REQUEST_COUNT.labels(method="POST", path=path, status=str(e.status_code)).inc()
        ERROR_COUNT.labels(path=path, reason="validation").inc()
        raise

    # Approximate context length (tokens) for KV pressure proxy.
    approx_tokens = sum(len(m.content) // 4 for m in body.messages)

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
                return StreamingResponse(
                    stream_worker(worker_url, worker_body),
                    media_type="text/event-stream",
                )

            # Non-streaming: try conversation-scoped prompt cache first, then worker.
            if not body.stream and body.conversation_id:
                messages_for_cache = [{"role": m.role, "content": m.content} for m in body.messages]
                cached, cache_hit_type = get_cached(body.conversation_id, body.model, messages_for_cache)
                if cached is not None and cache_hit_type:
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
                    return response

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
            return response
        except Exception:
            ERROR_COUNT.labels(path=path, reason="worker_error").inc()
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
    """Probe configured worker's /health. Returns worker status or 503 if no worker/unhealthy."""
    worker_url = get_worker_url(DEFAULT_WORKER_ID)
    if not worker_url:
        return {"status": "no_worker", "worker": None}
    try:
        data = await get_worker_health(worker_url)
        return {"status": "ok", **data}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Worker unreachable: {e!s}")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> Response:
    """Count 5xx and re-raise so FastAPI returns 500."""
    path = getattr(request.url, "path", "/v1/chat/completions")
    ERROR_COUNT.labels(path=path, reason="unhandled").inc()
    REQUEST_COUNT.labels(method=request.method, path=path, status="500").inc()
    raise exc

"""
Wave gateway: OpenAI-compatible /v1/chat/completions with tenant config,
Redis session store (conversation_id -> worker_id), and Prometheus metrics.
"""

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import PlainTextResponse

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


# Placeholder until Phase 2: single logical worker
DEFAULT_WORKER_ID = "worker-1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: optional Redis ping; shutdown: nothing for now
    yield


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

    # Affinity: lookup or assign worker for this conversation
    worker_id = None
    if body.conversation_id:
        worker_id = get_worker_for_conversation(body.conversation_id)
        if not worker_id:
            worker_id = DEFAULT_WORKER_ID
            set_worker_for_conversation(body.conversation_id, worker_id)

    # Phase 1 stub: no real worker call yet; return a placeholder response
    # Phase 2: proxy to worker_id's /generate
    latency_ms = int((time.perf_counter() - start) * 1000)
    tokens_in = sum(len(m.content) // 4 for m in body.messages)  # rough
    tokens_out = 10  # stub
    model_version = f"{body.model}-v1"
    cost_estimate = (tokens_in + tokens_out) * 0.000_001  # placeholder USD

    response = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        model=body.model,
        choices=[
            ChatChoice(
                message=ChatChoiceMessage(
                    role="assistant",
                    content="[Wave Phase 1 stub: no worker connected yet.]",
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


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> Response:
    """Count 5xx and re-raise so FastAPI returns 500."""
    path = getattr(request.url, "path", "/v1/chat/completions")
    ERROR_COUNT.labels(path=path, reason="unhandled").inc()
    REQUEST_COUNT.labels(method=request.method, path=path, status="500").inc()
    raise exc

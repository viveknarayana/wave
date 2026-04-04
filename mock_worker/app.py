"""
OpenAI-compatible mock for local / kind testing when vLLM CPU image is not used.
Implements /health and POST /v1/chat/completions (non-streaming; streaming returns minimal SSE).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Wave mock LLM worker")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _usage(messages: list) -> tuple[int, int]:
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = 8
    return prompt_tokens, completion_tokens


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    model = body.get("model", "mock-model")
    messages = body.get("messages") or []
    stream = body.get("stream", False)

    if stream:

        async def sse():
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": "[mock-worker] "}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            end = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(end)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    pt, ct = _usage(messages)
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "[mock-worker] OK — routed through Wave gateway + K8s."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
            },
        }
    )

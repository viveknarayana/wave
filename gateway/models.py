"""Request/response schemas for /v1/chat/completions (OpenAI-compatible + Wave extensions)."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible request with Wave extensions."""

    model: str
    messages: List[ChatMessage]
    tenant_id: Optional[str] = Field(default=None, description="Wave: tenant for config/rate limits")
    conversation_id: Optional[str] = Field(default=None, description="Wave: for affinity routing")
    priority: Optional[str] = Field(default="standard", description="Wave: standard | premium")

    # Optional OpenAI-style fields
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


class WaveUsage(BaseModel):
    """Wave-specific usage and cost attached to the response."""

    latency_ms: int
    tokens_in: int
    tokens_out: int
    model_version: str
    cost_estimate: float


class ChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible response with Wave usage."""

    id: str
    object: str = "chat.completion"
    model: str
    choices: List[ChatChoice]
    usage: Usage
    wave: Optional[WaveUsage] = None  # Wave extensions

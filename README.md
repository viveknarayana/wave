# Wave

![Wave](wave.png)

Experimentation with a Kubernetes-native LLM inference gateway that sits in front of vLLM and adds production features (routing, caching, tenancy, metrics).

See [LLM-Inference-Gateway-TODO.md](./LLM-Inference-Gateway-TODO.md) for the full roadmap.

---

## What Wave adds on top of vLLM

- **OpenAI-compatible gateway**: `POST /v1/chat/completions` with a stable API surface for clients.
- **Multi-tenancy hooks**: tenant model allow-lists and context limits via `tenant_id`.
- **Metrics**: Prometheus request QPS/latency/error counters (gateway-level).
- **Routing + affinity**:
  - Redis-backed session stickiness (`conversation_id -> worker_id`).
  - KV-pressure-aware worker selection for new conversations.
  - **Eviction + reroute** under KV pressure: unpin conversations from saturated workers (cold-start reroute to a healthier worker).
- **Priority scheduling** (gateway-level): for non-streaming calls, a small request-queue that prioritizes premium over free before dispatching to the worker.
- **Prompt caching** (conversation-scoped):
  - Exact cache: normalized prompt within `(conversation_id, model)`.
  - Optional semantic cache via embeddings (if `sentence-transformers` is installed).


# Wave

Production-grade Kubernetes-native LLM inference gateway with continuous batching, multi-layer prompt caching, KV-aware routing, and SLO-driven scaling.

See [LLM-Inference-Gateway-TODO.md](./LLM-Inference-Gateway-TODO.md) for the full roadmap.

---

## KV cache and affinity routing

### Where the KV cache lives

- **Per worker, in-process.** Each vLLM worker is a process with its own memory (GPU or CPU). While it handles a request, it fills a **KV cache** in that process: key-value pairs for every token in the context, so it can generate the next token without recomputing from scratch.
- **Not a separate service.** The cache lives inside the inference engine on each worker. Worker 1’s memory has its own KV; Worker 2’s memory has its own KV. They don’t share.

### Why one conversation stays on one worker

- When **Conversation A** is first sent to **Worker 1**, Worker 1 fills its KV cache with the context for A and streams the reply.
- The **next message** in Conversation A must go to **Worker 1** again so it can **reuse** that KV (and extend it). If you sent it to Worker 2, Worker 2 has no KV for A—it would have to rebuild the full context from scratch (slower, and doubles memory use across workers).
- So: **all KV for a conversation lives on exactly one worker.** The gateway keeps that conversation pinned to that worker via **affinity routing** (`conversation_id → worker_id` in Redis).

### What happens when a conversation moves to a new worker

When a conversation is handled by a **new** worker (e.g. the old worker died, or we evict/redistribute for capacity):

- That worker has **no KV** for that conversation.
- It must **rebuild** the full context (all previous messages) in one or more forward passes to fill its KV cache, then generate the reply.
- So the **first response on the new worker is slower** (and uses more compute/memory) than if the conversation had stayed on the original worker.

The gateway only moves a conversation to another worker when necessary; otherwise it uses affinity routing to reuse KV and avoid that extra latency.

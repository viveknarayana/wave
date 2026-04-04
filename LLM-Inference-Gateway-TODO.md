# LLM Inference Gateway ToDo List
**Kubernetes-oriented LLM gateway in front of vLLM: routing, caching, gateway metrics, SLO-style admission (in-process), CPU HPA manifests.**

## Phase 1: Core Gateway (Week 1)
### [x] Design API spec
- [x] `/v1/chat/completions` (OpenAI compatible)
- [x] Request schema: `model`, `messages`, `tenant_id`, `conversation_id`, `priority`
- [x] Response schema: `latency_ms`, `tokens_in`, `tokens_out`, `model_version`, `cost_estimate` (in `wave` object)

### [x] Build gateway service (FastAPI/Flask)
- [x] Request validation + tenant config lookup
- [x] Simple Redis-backed session store (`conversation_id -> worker_id`)
- [x] Basic metrics (Prometheus client): QPS, latency histograms, error rates

**Run:** `pip install -r requirements.txt && uvicorn gateway.main:app --reload --port 8080` (optional: `REDIS_URL=redis://localhost:6379/0`)

## Phase 2: Model Workers (Week 2)
### [x] Gateway → worker proxy
- [x] Gateway calls worker's `/v1/chat/completions` (OpenAI-compatible); `WORKER_BASE_URL` or `WORKER_1_URL` env
- [x] vLLM (CPU on M2, Python 3.12 + build from source) or TGI
- [x] Health: worker `/health`; gateway `GET /worker/health`; vLLM has `/metrics` for capacity
- [x] Streaming support: `stream: true` → gateway streams SSE from worker

### [x] Docker images
- [x] `Dockerfile.worker` (vLLM; env `MODEL_ID`, `PORT`)
- [x] `Dockerfile.gateway` (Wave gateway)

**Run (local, two terminals):**
```bash
# Terminal 1: vLLM worker (CPU on M2). Install vLLM first; then:
vllm serve Qwen/Qwen2-0.5B-Instruct --dtype=bfloat16 --port 8000

# Terminal 2: Gateway (from repo root)
pip install -r requirements.txt
WORKER_BASE_URL=http://localhost:8000 uvicorn gateway.main:app --reload --port 8080
```
Test: `curl -X POST ... -d '{"model":"Qwen/Qwen2-0.5B-Instruct","tenant_id":"premium","messages":[...]}'`. Stream: add `"stream":true`. Worker health: `curl http://localhost:8080/worker/health`

## Phase 3: Kubernetes Foundation (Week 2-3)
### [x] K8s manifests
- [x] `k8s/namespace.yaml`
- [x] `k8s/gateway-deployment.yaml` (3 replicas) + Service (NodePort 30080)
- [x] `k8s/worker-deployment.yaml` (1 replica; vLLM CPU via `vllm/vllm-openai-cpu`; optional GPU nodeSelector in comments)
- [x] `k8s/worker-service.yaml` (ClusterIP `worker:8000`)
- [x] `k8s/worker-deployment-mock.yaml` (optional stub for fast kind tests)
- [x] `k8s/redis-statefulset.yaml` + headless Service
- [x] `k8s/README.md` — apply order, build/load images, test commands

### [ ] Local cluster
- [ ] Create kind/k3d cluster, build & load `gateway:latest` and `llm-worker:latest`
- [ ] `kubectl apply -f k8s/` then wait for pods; `kubectl port-forward svc/gateway 8080:8080`
- [ ] Test end-to-end: `curl localhost:8080/v1/chat/completions ...`

## Phase 4: Continuous Batching (Week 3)
### [x] Gateway batching engine (priority-aware)
- [x] In-gateway priority batcher (premium vs free) for non-streaming requests
- [x] Small wait window (~10ms) and max batch size to group requests by model
- [x] Premium requests dispatched before free; actual kernel-level batching remains in vLLM

## Phase 5: KV-Aware Routing (Week 4)
### [x] Affinity routing
- [x] `conversation_id -> worker_id` mapping (Redis TTL 1hr) via session store
- [x] Load-aware worker selection (least KV pressure) for new conversations
- [x] Fallback: if no mapping, choose worker from KV-aware router (defaults to `worker-1` when single worker)

### [x] KV pressure metrics (proxy)
- [x] Per-worker proxy: `active_conversations * avg_context_tokens` tracked in-process
- [x] Reject routing to saturated workers (>80% KV capacity) — TODO: tie into admission control
 - [x] Implement KV eviction policy (evict oldest/lowest-priority conversations when >80% capacity)
 - [ ] Compare naive vs KV-aware eviction on max context length and p95 latency

## Phase 6: Semantic Prompt Caching (Week 4-5) ⭐
### [x] Exact prompt caching
- [x] Conversation-scoped: key = `hash(conversation_id, model, normalized_prompt)` (no cross-conversation reuse)
- [x] Redis-backed cache with TTL (`PROMPT_CACHE_TTL`, default 1hr); full response stored
- [ ] Measure hit rate, p95 latency, and cost reduction at 1, 10, 50 concurrent users

### [x] Semantic prompt caching
- [x] Embeddings via sentence-transformers (`all-MiniLM-L6-v2`); vectors stored per `(conversation_id, model)` in Redis (JSON list)
- [x] Similarity threshold `SEMANTIC_CACHE_THRESHOLD` (default 0.92); optional context = last N messages (`SEMANTIC_CONTEXT_MESSAGES=2`)
- [ ] Compare exact vs semantic caching on latency and qualitative answer quality

**Impl:** `gateway/prompt_cache.py` — `get_cached` / `set_cached`; `wave.cache_hit` = `"exact"` | `"semantic"`. Env: `ENABLE_PROMPT_CACHE=1`, `PROMPT_CACHE_TTL`, `SEMANTIC_CACHE_THRESHOLD`, `SEMANTIC_CONTEXT_MESSAGES`, `MAX_SEMANTIC_ENTRIES_PER_CONV`. Semantic is disabled if `sentence-transformers` is not installed.

## Phase 7: SLO-Driven Autoscaling (Week 5)
### [x] SLO definitions (thresholds enforced in gateway code; not cluster-wide SLAs)
```yaml
premium:  { p95_latency: "1s", error_rate: "0.1%" }
standard: { p95_latency: "3s", error_rate: "1%"   }
```

### [x] Custom metrics
- [ ] Prometheus queries for p95 latency per tenant tier
- [x] Admission control: reject low-priority if SLO violated
- [x] Add metric `wave_request_latency_ms_bucket{tenant_tier=...,status=...}` for percentile math
- [x] Add metric `wave_requests_total{tenant_tier=...,status=...}` for error-rate SLO tracking
- [x] Add metric `wave_admission_rejections_total{tenant_tier=...,reason=...}`
- [x] Expose `queue_depth` and `inflight_requests` gauges from gateway

### [x] HPA
- [x] HPA on CPU utilization (`k8s/*-hpa.yaml`; needs `metrics-server`)
- [ ] HPA on `queue_depth` / p95 (needs Prometheus Adapter or KEDA + metric rules)
- [ ] Scale-out: p95 > SLO * 1.2 for 2min
- [ ] Scale-in: p95 < SLO * 0.8 for 5min
- [ ] Deploy Prometheus Adapter or KEDA for custom metrics API
- [x] Add `k8s/gateway-hpa.yaml` (min=2, max=10, with stabilization windows)
- [x] Add `k8s/worker-hpa.yaml` (min=1, max=8, **CPU-only** metrics; same as gateway)
- [x] Add cooldown and max-step scale policy to avoid oscillation

**Impl target:**
- [x] Gateway SLO policy evaluator reads rolling p95 + error-rate per tier every 15s
- [x] Admission path rejects/de-prioritizes `free` requests first during SLO violations (`429` + retry hint)
- [ ] K8s custom metrics pipeline publishes `queue_depth` and `p95_latency_ms` for HPA

**Verify (commands):**
- [ ] `kubectl apply -f k8s/ && kubectl apply -f k8s/gateway-hpa.yaml && kubectl apply -f k8s/worker-hpa.yaml`
- [ ] `kubectl get hpa -n wave -w`
- [ ] `kubectl top pods -n wave`
- [ ] `kubectl logs -n wave deploy/gateway --tail=200 | rg "admission|slo|reject|p95"`

## Phase 8: Multi-Tenancy (Week 6)
### [ ] Tenant configs
```yaml
tenants:
  free:    { max_context: 2k, models: ["tiny"], rate_limit: 10/min }
  premium: { max_context: 32k, models: ["all"], rate_limit: 1000/min }
```

### [ ] Cost tracking
- [ ] Per-model token prices (USD/1M tokens)
- [ ] Per-request cost estimate
- [ ] Tenant budget enforcement (soft throttle)

## Phase 9: Polish & Demo (Week 6-7)
### [ ] Load testing
- [ ] `locust` script: 100 concurrent chat streams
- [ ] Measure: p95 latency, tokens/sec, cost/minute
- [ ] Before/after prompt caching impact (hit rate, latency, cost)

### [ ] Documentation
- [ ] `README`: architecture diagram, benchmarks, deploy instructions
- [ ] API docs (Swagger/OpenAPI)
- [ ] `demo.gif`: load test + Grafana dashboards

### [ ] Resume bullets (honest — fill in after you ship the unchecked phases)
```text
- LLM inference gateway (FastAPI) in front of vLLM: OpenAI-compatible API, streaming, Redis affinity,
  gateway-level priority batching for non-streaming requests, exact + semantic prompt cache, KV-pressure-style routing
- In-gateway SLO window + admission (reject free tier on violation); Prometheus /metrics; Kubernetes manifests + CPU HPA
- (Add after implemented: load-test numbers, budget enforcement, Temporal, speculative decoding, etc.)
```

## Resource Budget
- **Free**: Local kind cluster + CPU models
- **~$20**: 1x A10G on Runpod/Lambda Labs for 10hrs GPU testing
- **Time**: 6-7 weeks @ 10-15hrs/week

## Success Metrics (targets — not validated in this repo)
- p95 latency: <1s (premium), <3s (standard)
- GPU utilization: >80% under load (when running on GPU workers)
- Speculative decoding speedup: 1.5–2.5× tokens/sec (**not implemented here**)
- 100 concurrent users: no SLO violations (**not load-tested here**)

---

**Start with Phase 1-3 (gateway + K8s + basic workers) this weekend.**

# LLM Inference Gateway ToDo List
**"Production-grade Kubernetes-native LLM serving platform with speculative decoding, continuous batching, and SLO-driven scaling"**

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
- [x] `k8s/worker-deployment.yaml` (1 replica; optional GPU nodeSelector in comments)
- [x] `k8s/redis-statefulset.yaml` + headless Service
- [x] `k8s/README.md` — apply order, build/load images, test commands

### [ ] Local cluster
- [ ] Create kind/k3d cluster, build & load `gateway:latest` and `llm-worker:latest`
- [ ] `kubectl apply -f k8s/` then wait for pods; `kubectl port-forward svc/gateway 8080:8080`
- [ ] Test end-to-end: `curl localhost:8080/v1/chat/completions ...`

## Phase 4: Continuous Batching (Week 3)
### [ ] Gateway batching engine
- [ ] Queue incoming requests by model name
- [ ] Batch formation: max 4096 tokens OR 10ms timeout
- [ ] Fan-out to workers with batched payload
- [ ] De-batch responses back to individual streams

## Phase 5: KV-Aware Routing (Week 4)
### [ ] Affinity routing
- [ ] `conversation_id -> worker_id` mapping (Redis TTL 1hr)
- [ ] Load-aware worker selection (least active_requests)
- [ ] Fallback: rebuild context on worker failure

### [ ] KV pressure metrics
- [ ] Per-worker: `active_conversations * avg_context_length`
- [ ] Reject routing to saturated workers (>80% KV capacity)
 - [ ] Implement KV eviction policy (evict oldest/lowest-priority conversations when >80% capacity)
 - [ ] Compare naive vs KV-aware eviction on max context length and p95 latency

## Phase 6: Semantic Prompt Caching (Week 4-5) ⭐
### [ ] Exact prompt caching
- [ ] Normalized prompt + model as cache key (`hash(model, prompt)`)
- [ ] Redis-backed cache with TTL (e.g. 1hr) for full responses
- [ ] Measure hit rate, p95 latency, and cost reduction at 1, 10, 50 concurrent users

### [ ] Semantic prompt caching
- [ ] Embed prompts and store vectors in Redis (vector index)
- [ ] Define similarity threshold (e.g. ≥0.9) for cache hits
- [ ] Compare exact vs semantic caching on latency and qualitative answer quality

## Phase 7: SLO-Driven Autoscaling (Week 5)
### [ ] SLO definitions
```yaml
premium:  { p95_latency: "1s", error_rate: "0.1%" }
standard: { p95_latency: "3s", error_rate: "1%"   }
```

### [ ] Custom metrics
- [ ] Prometheus queries for p95 latency per tenant tier
- [ ] Admission control: reject low-priority if SLO violated

### [ ] HPA + custom scaler
- [ ] HPA on queue_depth + p95_latency
- [ ] Scale-out: p95 > SLO * 1.2 for 2min
- [ ] Scale-in: p95 < SLO * 0.8 for 5min

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

### [ ] Resume bullets (ready to copy-paste)
```text
- Engineered Kubernetes-native LLM inference gateway with continuous batching, multi-layer prompt caching
  (exact + semantic, up to 90% cost reduction), and SLO-driven autoscaling across multi-tenant workloads
- Implemented KV-cache-aware routing preserving conversation locality and paged eviction policies supporting 4x
  longer contexts without OOM
- Designed cost-aware admission control enforcing per-tenant budgets and model tier limits, integrated with
  Temporal for long-running LLM pipelines
```

## Resource Budget
- **Free**: Local kind cluster + CPU models
- **~$20**: 1x A10G on Runpod/Lambda Labs for 10hrs GPU testing
- **Time**: 6-7 weeks @ 10-15hrs/week

## Success Metrics (target these numbers)
- ✅ p95 latency: <1s (premium), <3s (standard)
- ✅ GPU utilization: >80% under load
- ✅ Speculative speedup: 1.5-2.5x tokens/sec
- ✅ 100 concurrent users: no SLO violations

---

**Start with Phase 1-3 (gateway + K8s + basic workers) this weekend.**

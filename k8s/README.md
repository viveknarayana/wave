# Wave Kubernetes manifests

Deploy order matters: **namespace first**, then Redis, worker, gateway (gateway expects Redis + worker Service DNS).

## Prerequisites

- `kubectl`, `docker`, **`kind`** (or k3d / any cluster with default StorageClass)
- For **CPU-only kind nodes**, use the **mock worker** path below. The default `Dockerfile.worker` (`pip install vllm`) often **fails** in slim images without CUDA (device inference errors).

## One-shot local test (recommended)

From repo root **`wave/`** (where `Dockerfile.*` and `k8s/` live):

```bash
bash scripts/kind-e2e.sh
```

This builds **`gateway:latest`** + **`llm-worker-mock:latest`**, loads them into kind, applies manifests in order, and points the worker Deployment at the mock image.

Then:

```bash
kubectl --context kind-wave -n wave port-forward svc/gateway 8080:8080
```

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/worker/health
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2-0.5B-Instruct","tenant_id":"premium","conversation_id":"k8s-test-1","messages":[{"role":"user","content":"Hi"}]}'
```

Check Redis affinity after the first request:

```bash
kubectl -n wave exec sts/redis -- redis-cli GET conv:k8s-test-1
# expect: worker-1
```

## Manual kind steps

```bash
kind create cluster --name wave
docker build -f Dockerfile.gateway -t gateway:latest .
docker build -f Dockerfile.worker-mock -t llm-worker-mock:latest .
kind load docker-image gateway:latest --name wave
kind load docker-image llm-worker-mock:latest --name wave
kubectl config use-context kind-wave

kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/gateway-deployment.yaml
kubectl -n wave set image deployment/worker vllm=llm-worker-mock:latest
kubectl -n wave rollout status deployment/worker --timeout=180s
kubectl -n wave rollout status deployment/gateway --timeout=180s
```

**Do not rely on** `kubectl apply -f k8s/` alone: alphabetical order can apply `gateway` before `namespace` exists.

## HPAs

`gateway-hpa.yaml` / `worker-hpa.yaml` target **CPU**. Install **metrics-server** or HPAs will log errors (harmless for routing tests).

## Gateway env notes

- **`ENABLE_PROMPT_CACHE=0`** is set in `gateway-deployment.yaml` so the first request does not lazy-load embedding models (slow / memory-heavy in small limits). Set to `1` when you want cache demos and enough memory.

## Real vLLM worker

Use a **GPU** node and an image that matches vLLM’s install docs, or build from upstream **CPU** Docker instructions. Keep `llm-worker:latest` in `worker-deployment.yaml` when your image works.

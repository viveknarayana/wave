# Wave Kubernetes manifests

Apply **namespace first**, then the rest (gateway expects Redis + `worker` Service DNS).

## Worker image options

### A) Official vLLM CPU image (default for real inference)

Matches [vLLM CPU install — pre-built images](https://docs.vllm.ai/en/stable/getting_started/installation/cpu.html#pre-built-images):

- **linux/arm64** (M1/M2 Mac, ARM kind nodes): `vllm/vllm-openai-cpu:latest-arm64`
- **linux/amd64**: `vllm/vllm-openai-cpu:latest-x86_64`

This repo’s **`Dockerfile.worker`** is a thin `FROM` of that image (pick tag via `VLLM_CPU_TAG`). **`k8s/worker-deployment.yaml`** passes `vllm serve` args (model, `--host`, `--port`, `--dtype bfloat16`), **`/dev/shm`**, `SYS_NICE`, relaxed seccomp per the same doc, and long **startup** probes (first model download can take many minutes).

### B) Mock worker (routing only, seconds)

`Dockerfile.worker-mock` + **`k8s/worker-deployment-mock.yaml`** — no vLLM, no weights.

## One-shot local test (kind)

From **`wave/`** (directory with `Dockerfile.*` and `k8s/`):

```bash
# Real vLLM CPU (first run: large image + model download; be patient)
bash scripts/kind-e2e.sh

# Or fast stub
WAVE_WORKER=mock bash scripts/kind-e2e.sh
```

The script picks **arm64 vs x86_64** tag from `uname -m`, builds `gateway:latest`, builds or loads the worker image, `kind load`s, applies manifests in order, and waits for rollouts (worker timeout **20 minutes** for vLLM CPU).

## Manual apply order

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/worker-service.yaml
kubectl apply -f k8s/worker-deployment.yaml   # or worker-deployment-mock.yaml
kubectl apply -f k8s/gateway-deployment.yaml
```

**Do not** rely on `kubectl apply -f k8s/` only: lexical order can apply gateway before the namespace exists.

## After deploy

```bash
kubectl -n wave port-forward svc/gateway 8080:8080
```

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/worker/health
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2-0.5B-Instruct","tenant_id":"premium","conversation_id":"k8s-test-1","messages":[{"role":"user","content":"Hi"}]}'
```

Redis affinity (after first request with `conversation_id`):

```bash
kubectl -n wave exec sts/redis -- redis-cli GET conv:k8s-test-1
```

## Build worker image locally

```bash
# ARM64 (default in Dockerfile)
docker build -f Dockerfile.worker -t llm-worker:latest .

# x86_64 / amd64 kind
docker build -f Dockerfile.worker --build-arg VLLM_CPU_TAG=latest-x86_64 -t llm-worker:latest .
```

## Gateway env

`gateway-deployment.yaml` sets **`ENABLE_PROMPT_CACHE=0`** so pods do not lazy-load embedding models on first request (heavy for small limits). Set to `1` when you want cache demos and enough memory.

### Multi-worker (local Docker + gateway on host)

Do **not** set `WORKER_BASE_URL` when you want more than one backend; use numbered URLs so the gateway can route by `worker-1`, `worker-2`, … and Redis affinity (`conv:<conversation_id>`):

```bash
export REDIS_URL=redis://127.0.0.1:6379/0
export WORKER_1_URL=http://127.0.0.1:8000
export WORKER_2_URL=http://127.0.0.1:8001
export ENABLE_PROMPT_CACHE=0
# from wave/ (repo root of Dockerfiles)
uvicorn gateway.main:app --port 8080
```

**Recommended on a laptop:** one real vLLM on **:8000** plus the mock on **:8001** — same `WORKER_*_URL` env as above; start the mock with:

```bash
docker compose -f docker-compose.vllm-and-mock.yaml up -d --build
```

(Run vLLM however you already do; omit the compose file’s `vllm` profile so you do not start a second vLLM.) Optional **two full vLLM** CPUs: `docker-compose.two-vllm.yaml` (heavy).

```bash
curl -s http://localhost:8080/worker/health
```

New conversations pick the least-loaded worker (KV pressure proxy); repeating the same `conversation_id` sticks to the worker stored in Redis. Replies that contain **`[mock-worker]`** came from the mock; real model text comes from vLLM.

## HPAs

`gateway-hpa.yaml` / `worker-hpa.yaml` use **CPU** targets. Install **metrics-server** or ignore HPA warning events for local tests.

## GPU vLLM

Use an official CUDA/GPU vLLM image and adjust `worker-deployment.yaml` (resources, nodeSelector, image); this folder is oriented around **CPU** kind setups.

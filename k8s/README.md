# Wave Kubernetes manifests (Phase 3)

Deploy order: namespace → Redis → worker → gateway (gateway needs Redis and worker).

## Prerequisites

- **kubectl** installed
- **kind** or **k3d** (or any cluster with default StorageClass for Redis volume)

## 1. Create cluster (kind example)

```bash
kind create cluster --name wave
```

## 2. Build and load images (so the cluster can pull them)

From repo root:

```bash
docker build -f Dockerfile.gateway -t gateway:latest .
docker build -f Dockerfile.worker -t llm-worker:latest .

kind load docker-image gateway:latest --name wave
kind load docker-image llm-worker:latest --name wave
```

(With **k3d**: `k3d image import gateway:latest llm-worker:latest -c k3d-default` or similar.)

## 3. Apply manifests

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/gateway-deployment.yaml
```

Or all at once:

```bash
kubectl apply -f k8s/
```

## 4. Wait for pods

```bash
kubectl -n wave get pods -w
```

Wait until `gateway`, `worker`, and `redis-0` are Running.

## 5. Test

Gateway is exposed as **NodePort 30080**. With kind, port-forward or use the node IP:

```bash
# Port-forward (works from any cluster)
kubectl -n wave port-forward svc/gateway 8080:8080
```

Then:

```bash
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2-0.5B-Instruct","tenant_id":"premium","messages":[{"role":"user","content":"Hi"}]}' | python3 -m json.tool
```

If using NodePort directly (e.g. kind node is localhost):

```bash
curl -s http://localhost:30080/health
```

## Notes

- **Redis**: StatefulSet with 1Gi volume. Needs default StorageClass (kind provides one).
- **Worker**: 1 replica, CPU-only by default. For GPU, set nodeSelector/tolerations and use a GPU image.
- **Gateway**: 3 replicas, talks to `redis:6379` and `http://worker:8000` inside the cluster.

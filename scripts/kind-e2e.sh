#!/usr/bin/env bash
# End-to-end Wave on kind: builds gateway + mock worker (vLLM image often fails on CPU-only nodes).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:${PATH:-}"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-wave}"

kind get clusters | grep -q "^${CLUSTER_NAME}$" || kind create cluster --name "${CLUSTER_NAME}"

docker build -f Dockerfile.gateway -t gateway:latest .
docker build -f Dockerfile.worker-mock -t llm-worker-mock:latest .

kind load docker-image gateway:latest --name "${CLUSTER_NAME}"
kind load docker-image llm-worker-mock:latest --name "${CLUSTER_NAME}"

CTX="kind-${CLUSTER_NAME}"
kubectl config use-context "${CTX}"

kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/gateway-deployment.yaml
# HPAs need metrics-server; optional for routing smoke test
kubectl apply -f k8s/gateway-hpa.yaml 2>/dev/null || true
kubectl apply -f k8s/worker-hpa.yaml 2>/dev/null || true

kubectl -n wave set image deployment/worker vllm=llm-worker-mock:latest
kubectl -n wave rollout status deployment/worker --timeout=180s
kubectl -n wave rollout status deployment/gateway --timeout=180s

echo ""
echo "Pods:"
kubectl -n wave get pods
echo ""
echo "Next (separate terminal): kubectl -n wave port-forward svc/gateway 8080:8080"
echo "Then:"
echo '  curl -s http://localhost:8080/health'
echo '  curl -s http://localhost:8080/worker/health'
echo '  curl -s -X POST http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \'
echo '    -d '"'"'{"model":"Qwen/Qwen2-0.5B-Instruct","tenant_id":"premium","conversation_id":"k8s-test-1","messages":[{"role":"user","content":"Hi"}]}'"'"''
echo ""
echo "Redis affinity key (after first request): kubectl -n wave exec sts/redis -- redis-cli GET conv:k8s-test-1"

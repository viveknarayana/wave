#!/usr/bin/env bash
# End-to-end Wave on kind.
#
# WAVE_WORKER=vllm-cpu (default): official vLLM CPU image per
#   https://docs.vllm.ai/en/stable/getting_started/installation/cpu.html#pre-built-images
# WAVE_WORKER=mock: fast OpenAI-shaped stub (no model download).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:${PATH:-}"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-wave}"
WAVE_WORKER="${WAVE_WORKER:-vllm-cpu}"

kind get clusters | grep -q "^${CLUSTER_NAME}$" || kind create cluster --name "${CLUSTER_NAME}"

docker build -f Dockerfile.gateway -t gateway:latest .

if [[ "$WAVE_WORKER" == "mock" ]]; then
  docker build -f Dockerfile.worker-mock -t llm-worker-mock:latest .
  kind load docker-image gateway:latest --name "${CLUSTER_NAME}"
  kind load docker-image llm-worker-mock:latest --name "${CLUSTER_NAME}"
else
  case "$(uname -m)" in
    arm64 | aarch64) VLLM_CPU_TAG=latest-arm64 ;;
    *) VLLM_CPU_TAG=latest-x86_64 ;;
  esac
  echo "Using vLLM CPU image tag: ${VLLM_CPU_TAG} (see Dockerfile.worker)"
  docker build -f Dockerfile.worker --build-arg "VLLM_CPU_TAG=${VLLM_CPU_TAG}" -t llm-worker:latest .
  kind load docker-image gateway:latest --name "${CLUSTER_NAME}"
  kind load docker-image llm-worker:latest --name "${CLUSTER_NAME}"
fi

CTX="kind-${CLUSTER_NAME}"
kubectl config use-context "${CTX}"

kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/worker-service.yaml
if [[ "$WAVE_WORKER" == "mock" ]]; then
  kubectl apply -f k8s/worker-deployment-mock.yaml
else
  kubectl apply -f k8s/worker-deployment.yaml
fi
kubectl apply -f k8s/gateway-deployment.yaml
kubectl apply -f k8s/gateway-hpa.yaml 2>/dev/null || true
kubectl apply -f k8s/worker-hpa.yaml 2>/dev/null || true

echo "Waiting for worker (vLLM CPU first boot can take many minutes to download weights)..."
kubectl -n wave rollout status deployment/worker --timeout=1200s
kubectl -n wave rollout status deployment/gateway --timeout=180s

echo ""
echo "Pods:"
kubectl -n wave get pods
echo ""
echo "Next: kubectl -n wave port-forward svc/gateway 8080:8080"
echo "Then curl /health, /worker/health, POST /v1/chat/completions (see k8s/README.md)"
if [[ "$WAVE_WORKER" != "mock" ]]; then
  echo ""
  echo "Worker logs (model load): kubectl -n wave logs deploy/worker -f"
fi

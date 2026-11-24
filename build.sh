#!/usr/bin/env bash
# build.sh  --  same as build.bat but for Mac/Linux/WSL

set -euo pipefail

API_IMAGE=image-api:1.0
WORKER_IMAGE=image-worker:1.0
NS=imgproc
ACTION=${1:-all}

build() {
  echo; echo "=== Building Docker images ==="; echo
  docker build -t "$API_IMAGE"    -f api/Dockerfile    .
  docker build -t "$WORKER_IMAGE" -f worker/Dockerfile .
  context=$(kubectl config current-context)
  if [[ "$context" == "minikube" ]]; then
    echo "Loading images into Minikube..."
    minikube image load "$API_IMAGE"
    minikube image load "$WORKER_IMAGE"
  elif [[ "$context" == "docker-desktop" ]]; then
    echo "Importing images into Docker Desktop Kubernetes..."
    bash scripts/load_docker_desktop_images.sh
  else
    echo "Unsupported or missing Kubernetes context: $context" >&2
    echo "Select the Minikube or docker-desktop context before building." >&2
    return 1
  fi
}

apply() {
  echo; echo "=== Applying k8s manifests ==="; echo
  kubectl delete hpa image-worker-hpa -n "$NS" --ignore-not-found
  kubectl apply -f k8s/00-base.yaml
  kubectl apply -f k8s/10-redis.yaml
  kubectl apply -f k8s/20-api.yaml
  kubectl apply -f k8s/30-worker.yaml
  kubectl rollout restart deployment/image-api deployment/image-worker -n "$NS"
  kubectl rollout status deployment/image-api -n "$NS" --timeout=90s
  kubectl rollout status deployment/image-worker -n "$NS" --timeout=90s
  echo
  echo "=== Deploy complete! ==="
  if [[ "$(kubectl config current-context)" == "minikube" ]]; then
    echo "Run:  minikube service image-api -n $NS"
  else
    echo "Run:  kubectl port-forward svc/image-api 30080:8000 -n $NS"
    echo "Open: http://localhost:30080"
  fi
}

case "$ACTION" in
  build) build ;;
  apply) apply ;;
  all)   build; apply ;;
  logs)  kubectl logs -n "$NS" -l app=image-worker --all-containers --follow --prefix=true ;;
  reset)
    read -rp "Delete namespace '$NS' and all data? Type YES: " ans
    [[ $ans == YES ]] || { echo Cancelled; exit 0; }
    kubectl delete namespace "$NS" --ignore-not-found
    kubectl delete pv imgproc-storage-pv --ignore-not-found
    echo "Namespace deleted. Run './build.sh apply' to redeploy."
    ;;
  *) echo "Usage: $0 [build|apply|logs|reset|all]"; exit 1 ;;
esac

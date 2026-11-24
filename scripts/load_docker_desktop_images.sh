#!/usr/bin/env bash
set -euo pipefail

context=$(kubectl config current-context)
[[ "$context" == "docker-desktop" ]] || { echo "Expected docker-desktop context; found '$context'." >&2; exit 1; }

archive=".pixelforge-k8s-images-$$.tar"
debug_pods=()
cleanup() {
  for pod in "${debug_pods[@]}"; do kubectl delete pod "$pod" -n default --ignore-not-found >/dev/null 2>&1 || true; done
  rm -f -- "$archive"
}
trap cleanup EXIT

docker save -o "$archive" image-api:1.0 image-worker:1.0
read -r -a nodes <<< "$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')"
(( ${#nodes[@]} > 0 )) || { echo "No nodes found in the active Kubernetes cluster." >&2; exit 1; }

for node in "${nodes[@]}"; do
  output=$(kubectl debug "node/$node" --image=busybox:1.36 --profile=sysadmin -- /bin/sh -c 'sleep 900')
  pod=$(sed -n 's/.*Creating debugging pod \([^ ]*\).*/\1/p' <<< "$output")
  [[ -n "$pod" ]] || { echo "Could not identify image-loader pod: $output" >&2; exit 1; }
  debug_pods+=("$pod")
  kubectl wait --for=condition=Ready "pod/$pod" -n default --timeout=90s
  kubectl cp "$archive" "default/$pod:/host/tmp/pixelforge-k8s-images.tar"
  kubectl exec -n default "$pod" -- chroot /host ctr -n k8s.io images import /tmp/pixelforge-k8s-images.tar
  kubectl exec -n default "$pod" -- chroot /host rm -f /tmp/pixelforge-k8s-images.tar
done

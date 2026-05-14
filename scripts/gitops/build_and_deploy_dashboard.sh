#!/usr/bin/env bash
set -euo pipefail

# build_and_deploy_dashboard.sh — Build and deploy the demo dashboard to Kubernetes
#
# This script:
#   1. Builds the dashboard Docker image using buildx
#   2. Pushes to Artifact Registry
#   3. Renders the deployment manifest with that image
#   4. Applies the rendered manifest and waits for rollout to complete
#
# Environment variables (required):
#   DASHBOARD_IMAGE           # Full image URI (e.g., europe-west2-docker.pkg.dev/...)
#   DASHBOARD_NAMESPACE       # K8s namespace (default: rollout-demo)
#   DASHBOARD_DEPLOYMENT      # Deployment name (default: rollout-demo-dashboard)

DASHBOARD_IMAGE="${DASHBOARD_IMAGE:?DASHBOARD_IMAGE not set}"
DASHBOARD_NAMESPACE="${DASHBOARD_NAMESPACE:-rollout-demo}"
DASHBOARD_DEPLOYMENT="${DASHBOARD_DEPLOYMENT:-rollout-demo-dashboard}"

echo "==================================================================="
echo "Building and Deploying Dashboard"
echo "==================================================================="
echo "Image: ${DASHBOARD_IMAGE}"
echo "Namespace: ${DASHBOARD_NAMESPACE}"
echo "Deployment: ${DASHBOARD_DEPLOYMENT}"
echo ""

# Create namespace if it doesn't exist
echo "Creating namespace if needed..."
kubectl create namespace "${DASHBOARD_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

rendered_dir="$(mktemp -d)"
trap 'rm -rf "${rendered_dir}"' EXIT
image_name="${DASHBOARD_IMAGE%:*}"
image_tag="${DASHBOARD_IMAGE##*:}"

# Apply a rendered Kustomize overlay with the requested image. The source
# Deployment keeps a logical image name; image selection belongs to Kustomize.
echo "Rendering and applying deployment manifest..."
cp demo-dashboard/k8s/deployment.yaml "${rendered_dir}/deployment.yaml"
cat > "${rendered_dir}/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
images:
  - name: rollout-demo-dashboard
    newName: ${image_name}
    newTag: ${image_tag}
EOF
kubectl apply -k "${rendered_dir}"

# Wait for deployment to exist
echo "Waiting for deployment to exist..."
for i in {1..30}; do
  if kubectl get deployment -n "${DASHBOARD_NAMESPACE}" "${DASHBOARD_DEPLOYMENT}" &>/dev/null; then
    echo "✓ Deployment exists"
    break
  fi
  echo "  [$i/30] Waiting for deployment..."
  sleep 2
done

# Wait for rollout
echo ""
echo "Waiting for rollout to complete..."
if kubectl -n "${DASHBOARD_NAMESPACE}" rollout status deployment/"${DASHBOARD_DEPLOYMENT}" --timeout=5m; then
  echo "✓ Rollout completed successfully"
else
  echo "✗ Rollout timeout"
  exit 1
fi

# Verify deployment
echo ""
echo "Verifying deployment..."
kubectl -n "${DASHBOARD_NAMESPACE}" get deployment "${DASHBOARD_DEPLOYMENT}" -o wide
kubectl -n "${DASHBOARD_NAMESPACE}" get pods -l app=rollout-demo-dashboard -o wide

# Get pod info
POD=$(kubectl -n "${DASHBOARD_NAMESPACE}" get pod -l app=rollout-demo-dashboard -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "${POD}" ]; then
  echo ""
  echo "Pod Information:"
  echo "  Name: ${POD}"
  echo "  Image:"
  kubectl -n "${DASHBOARD_NAMESPACE}" get pod "${POD}" -o jsonpath='{.spec.containers[0].image}'
  echo ""
  echo "Pod Events (last 5):"
  kubectl -n "${DASHBOARD_NAMESPACE}" describe pod "${POD}" | grep -A 20 "Events:" || true
fi

echo ""
echo "==================================================================="
echo "✓ Dashboard deployment completed successfully"
echo "==================================================================="
echo ""
echo "To access the dashboard:"
echo "  kubectl -n ${DASHBOARD_NAMESPACE} port-forward svc/rollout-demo-dashboard 8088:80"
echo ""
echo "Then visit: http://localhost:8088"

#!/usr/bin/env bash
set -euo pipefail
# trigger_rollout.sh — Simulate a CI/CD pipeline that creates an OrchestratedRollout
# CR when a new version tag is pushed.

# Usage:
#   ./scripts/trigger_rollout.sh v2.0.0
#   ./scripts/trigger_rollout.sh v1.0.0   # roll back

VERSION="${1:?Usage: trigger_rollout.sh <version-tag>}"
NAMESPACE="${NAMESPACE:-orchestrated-rollout}"
DEMO_DELAY="${DEMO_DELAY:-30}"   # seconds to pause before shifting traffic (override default 120s)

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="${REGION:-europe-west2}"
AR_REPO="${AR_REPO:-oroll}"
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/orchestrated-rollout-workload"
INGRESS_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "unknown")

# Pick a colour theme per version so the browser shows a clear visual difference
case "${VERSION}" in
  v1*) BG_GRADIENT="#667eea, #764ba2" ;;  # purple — original
  v2*) BG_GRADIENT="#11998e, #38ef7d" ;;  # green  — new
  v3*) BG_GRADIENT="#f7971e, #ffd200" ;;  # amber  — future
  *)   BG_GRADIENT="#e52d27, #b31217" ;;  # red    — unknown
esac

echo "===================================================="
echo "  Orchestrated Rollout — CI/CD Pipeline Trigger    "
echo "===================================================="
echo ""
echo "  New version:  ${VERSION}"
echo "  Image:        ${IMAGE_REPO}:${VERSION}"
echo "  Colour theme: ${BG_GRADIENT}"
echo "  Namespace:    ${NAMESPACE}"
echo "  Demo delay:   ${DEMO_DELAY}s  (set DEMO_DELAY=120 for full experiment)"
echo ""

# 1. Verify the image exists in Artifact Registry
echo "▶ Step 1: Verifying image exists in Artifact Registry..."
if ! gcloud artifacts docker images describe "${IMAGE_REPO}:${VERSION}" --quiet >/dev/null 2>&1; then
  echo "  ✗ Image ${IMAGE_REPO}:${VERSION} not found in AR."
  echo "    Build and push first: docker build + docker push"
  exit 1
fi
echo "  ✓ Image verified in AR"

# 2. Clean up any previous OrchestratedRollout CRs
echo "▶ Step 2: Cleaning up previous rollouts..."
kubectl delete orchestratedrollout --all -n "${NAMESPACE}" 2>/dev/null || true
kubectl delete rollout --all -n "${NAMESPACE}" 2>/dev/null || true
sleep 2
echo "  ✓ Clean slate"

# 3. Prepare the stable deployment with the new version's colour / args.
#    The Argo Rollout copies these args onto canary pods so they display correctly.
echo "▶ Step 3: Updating deployment args for ${VERSION}..."
helm upgrade workload charts/workload \
  --namespace "${NAMESPACE}" \
  --reuse-values \
  --set "image.repository=${IMAGE_REPO}" \
  --set "image.tag=${VERSION}" \
  --set "image.pullPolicy=Always" \
  --set "workload.version=${VERSION}" \
  --set "workload.bgGradient=${BG_GRADIENT/,/\\,}" \
  --wait --timeout 60s \
  2>&1 | tail -1
echo "  ✓ Deployment args updated"

# 4. Create the OrchestratedRollout CR — RL controller decides the strategy
echo "▶ Step 4: Creating OrchestratedRollout CR..."
cat <<EOF | kubectl apply -f -
apiVersion: rollout.orchestrated.io/v1alpha1
kind: OrchestratedRollout
metadata:
  name: workload-${VERSION}-rollout
  namespace: ${NAMESPACE}
  labels:
    app: workload
    version: "${VERSION}"
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: workload-workload
  release:
    image: ${IMAGE_REPO}
    tag: "${VERSION}"
  actionSet:
    - delay
    - pre-scale
    - canary
    - rolling
  slo:
    maxP95LatencyMs: 100
    maxErrorRate: 0.01
  rolloutHints:
    targetReplicas: 4
    warmUpClass: medium
    objective: reliability
  guardrailConfig:
    maxDelaySeconds: ${DEMO_DELAY}
    maxExtraReplicas: 5
    maxRolloutTimeSeconds: 600
EOF
echo "  ✓ CR created — RL controller deciding strategy now..."

# 5. Stream the controller's decision
echo ""
echo "▶ Step 5: Controller decision pipeline"
echo "────────────────────────────────────────────────────"
sleep 3
kubectl logs deploy/controller-controller -n "${NAMESPACE}" --tail=50 \
  | sed '/healthz/d; /kopf\.objects/d'
echo "────────────────────────────────────────────────────"

# 6. Show CR status
echo ""
echo "▶ Step 6: Rollout status"
kubectl get orchestratedrollout "workload-${VERSION}-rollout" -n "${NAMESPACE}" \
  -o jsonpath='{.status}' 2>/dev/null | python3 -m json.tool || echo "(pending...)"

echo ""
echo "  Browser: http://${INGRESS_IP}/"
echo "  Refresh the page — you will see ${VERSION} with a new colour as traffic shifts."
echo ""
echo "  Watch live progress:"
echo "    kubectl argo rollouts get rollout workload-workload-rollout -n ${NAMESPACE} --watch"

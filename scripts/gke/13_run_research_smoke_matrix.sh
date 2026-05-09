#!/usr/bin/env bash
set -euo pipefail

# 13_run_research_smoke_matrix.sh — bounded live validation before the long matrix.
#
# This intentionally does not replace the publication-grade comparison grid.
# It runs a short ML workload matrix that proves the live path across traffic
# shapes and optionally one Litmus fault without taking hours.

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west2}"
AR_REPO="${AR_REPO:-oroll}"
NAMESPACE="${NAMESPACE:-orchestrated-rollout}"
ML_RELEASE="${ML_RELEASE:-ml-workload}"
ML_IMAGE_REPO="${ML_IMAGE_REPO:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/orchestrated-rollout-ml-workload}"
ML_IMAGE_TAG="${ML_IMAGE_TAG:-e2e_20260428}"
ML_RELEASE_TAG="${ML_RELEASE_TAG:-v1.0.0}"
INGRESS_HOST_HEADER="${INGRESS_HOST_HEADER:-ml-workload.local}"
INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP:-$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}')}"

TIMESTAMP="${SMOKE_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
RESULTS_BASE="${RESULTS_BASE:-experiments/research_smoke_${TIMESTAMP}}"
REPEATS="${REPEATS:-1}"
SLO_P95_MS="${SLO_P95_MS:-500}"
K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS:-60}"
RUN_FAULT_SMOKE="${RUN_FAULT_SMOKE:-1}"
RUN_NO_FAULT_SMOKE="${RUN_NO_FAULT_SMOKE:-1}"
DEPLOY_ML_WORKLOAD="${DEPLOY_ML_WORKLOAD:-1}"

CHAOS_SERVICEACCOUNT="${CHAOS_SERVICEACCOUNT:-oroll-litmus-admin}"
CHAOS_CLUSTERROLE="${CHAOS_CLUSTERROLE:-oroll-litmus-admin}"
LITMUS_OPERATOR_VERSION="${LITMUS_OPERATOR_VERSION:-3.28.0}"
LITMUS_OPERATOR_IMAGE="${LITMUS_OPERATOR_IMAGE:-litmuschaos.docker.scarf.sh/litmuschaos/chaos-operator:${LITMUS_OPERATOR_VERSION}}"
LITMUS_RUNNER_IMAGE="${LITMUS_RUNNER_IMAGE:-litmuschaos.docker.scarf.sh/litmuschaos/chaos-runner:${LITMUS_OPERATOR_VERSION}}"

WORKLOAD_DEPLOYMENT_SELECTOR="${WORKLOAD_DEPLOYMENT_SELECTOR:-app.kubernetes.io/name=ml-workload,app.kubernetes.io/instance=${ML_RELEASE}}"
WORKLOAD_DEPLOYMENT_NAME="${WORKLOAD_DEPLOYMENT_NAME:-${ML_RELEASE}-ml-workload}"

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID is empty and could not be read from gcloud config." >&2
  exit 2
fi
if [ -z "${INGRESS_EXTERNAL_IP}" ]; then
  echo "ERROR: INGRESS_EXTERNAL_IP is empty and could not be discovered." >&2
  exit 2
fi

prepare_chaos_namespace() {
  if ! kubectl get crd chaosengines.litmuschaos.io >/dev/null 2>&1; then
    echo "Installing Litmus ChaosEngine/ChaosExperiment/ChaosResult CRDs..."
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/chaos_crds.yaml"
    kubectl wait --for=condition=Established crd/chaosengines.litmuschaos.io --timeout=60s
    kubectl wait --for=condition=Established crd/chaosexperiments.litmuschaos.io --timeout=60s
    kubectl wait --for=condition=Established crd/chaosresults.litmuschaos.io --timeout=60s
  fi

  if ! kubectl get deployment litmus -n litmus >/dev/null 2>&1; then
    echo "Installing Litmus chaos-operator..."
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/rbac.yaml"
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/operator.yaml"
  fi
  kubectl set image deployment/litmus -n litmus "chaos-operator=${LITMUS_OPERATOR_IMAGE}" >/dev/null
  kubectl set env deployment/litmus -n litmus "CHAOS_RUNNER_IMAGE=${LITMUS_RUNNER_IMAGE}" >/dev/null
  kubectl rollout status deployment/litmus -n litmus --timeout=180s

  echo "Preparing Litmus resources in ${NAMESPACE}..."
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  if ! kubectl get clusterrole "${CHAOS_CLUSTERROLE}" >/dev/null 2>&1; then
    cat <<EOF | kubectl apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${CHAOS_CLUSTERROLE}
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, pods/exec, events, configmaps, secrets, services]
    verbs: [get, list, watch, create, update, patch, delete, deletecollection]
  - apiGroups: ["apps"]
    resources: [deployments, replicasets, statefulsets, daemonsets]
    verbs: [get, list, watch, update, patch]
  - apiGroups: ["batch"]
    resources: [jobs]
    verbs: [get, list, watch, create, delete, deletecollection]
  - apiGroups: ["litmuschaos.io"]
    resources: [chaosengines, chaosexperiments, chaosresults]
    verbs: [get, list, watch, create, update, patch, delete]
EOF
  fi

  kubectl create serviceaccount "${CHAOS_SERVICEACCOUNT}" -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${CHAOS_CLUSTERROLE}-${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${CHAOS_CLUSTERROLE}
subjects:
  - kind: ServiceAccount
    name: ${CHAOS_SERVICEACCOUNT}
    namespace: ${NAMESPACE}
EOF

  kubectl apply -f k8s/chaos/generic-experiments.yaml -n "${NAMESPACE}"
}

run_ml_trial() {
  local label="$1"
  local scenario="$2"
  local fault="$3"
  shift 3

  local result_dir="${RESULTS_BASE}/${label}"
  echo ""
  echo "=============================================="
  echo "  Smoke: ${label}"
  echo "  Scenario: ${scenario}"
  echo "  Fault: ${fault}"
  echo "  Results: ${result_dir}"
  echo "=============================================="

  env \
    "$@" \
    NAMESPACE="${NAMESPACE}" \
    WORKLOAD_DEPLOYMENT_SELECTOR="${WORKLOAD_DEPLOYMENT_SELECTOR}" \
    WORKLOAD_DEPLOYMENT_NAME="${WORKLOAD_DEPLOYMENT_NAME}" \
    CHAOS_APP_LABEL="${WORKLOAD_DEPLOYMENT_SELECTOR}" \
    CHAOS_SERVICEACCOUNT="${CHAOS_SERVICEACCOUNT}" \
    INGRESS_HOST_HEADER="${INGRESS_HOST_HEADER}" \
    INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP}" \
    RELEASE_IMAGE="${ML_IMAGE_REPO}" \
    RELEASE_TAG="${ML_RELEASE_TAG}" \
    SLO_P95_MS="${SLO_P95_MS}" \
    K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS}" \
    K6_SAVE_RAW_JSON=0 \
    EXPERIMENT_ID="research_smoke_${label}_${TIMESTAMP}" \
    RESULTS_DIR="${result_dir}" \
    ./scripts/run_experiment.sh "${scenario}" "${fault}" "${REPEATS}"
}

mkdir -p "${RESULTS_BASE}"

echo "=============================================="
echo "  Research-Readiness Smoke Matrix"
echo "=============================================="
echo "  Namespace:      ${NAMESPACE}"
echo "  ML image:       ${ML_IMAGE_REPO}:${ML_IMAGE_TAG}"
echo "  Release tag:    ${ML_RELEASE_TAG}"
echo "  Ingress:        ${INGRESS_EXTERNAL_IP} (Host: ${INGRESS_HOST_HEADER})"
echo "  Results:        ${RESULTS_BASE}"
echo "  No-fault smoke: ${RUN_NO_FAULT_SMOKE}"
echo "  Fault smoke:    ${RUN_FAULT_SMOKE}"
echo "=============================================="

if [ "${DEPLOY_ML_WORKLOAD}" = "1" ]; then
  helm upgrade --install "${ML_RELEASE}" charts/ml-workload \
    --namespace "${NAMESPACE}" --create-namespace \
    --set image.repository="${ML_IMAGE_REPO}" \
    --set image.tag="${ML_IMAGE_TAG}" \
    --set image.pullPolicy=IfNotPresent \
    --set ingress.host="${INGRESS_HOST_HEADER}" \
    --wait --timeout 10m
fi

if [ "${RUN_NO_FAULT_SMOKE}" = "1" ]; then
  run_ml_trial "ml_steady_none" "steady" "none" \
    TARGET_RPS="${ML_STEADY_RPS:-10}" \
    DURATION="${ML_STEADY_DURATION:-2m}"

  run_ml_trial "ml_ramp_none" "ramp" "none" \
    BASE_RPS="${ML_RAMP_BASE_RPS:-5}" \
    PEAK_RPS="${ML_RAMP_PEAK_RPS:-25}" \
    RAMP_DURATION="${ML_RAMP_DURATION:-45s}" \
    HOLD_DURATION="${ML_RAMP_HOLD_DURATION:-45s}" \
    COOLDOWN_DURATION="${ML_RAMP_COOLDOWN_DURATION:-30s}"

  run_ml_trial "ml_spike_none" "spike" "none" \
    BASE_RPS="${ML_SPIKE_BASE_RPS:-5}" \
    SPIKE_RPS="${ML_SPIKE_RPS:-30}" \
    WARMUP_DURATION="${ML_SPIKE_WARMUP_DURATION:-30s}" \
    SPIKE_RAMP="${ML_SPIKE_RAMP:-10s}" \
    SPIKE_DURATION="${ML_SPIKE_DURATION:-30s}" \
    RECOVERY_RAMP="${ML_SPIKE_RECOVERY_RAMP:-10s}" \
    RECOVERY_DURATION="${ML_SPIKE_RECOVERY_DURATION:-40s}"
fi

if [ "${RUN_FAULT_SMOKE}" = "1" ]; then
  prepare_chaos_namespace
  run_ml_trial "ml_steady_pod-kill" "steady" "pod-kill" \
    TARGET_RPS="${ML_FAULT_RPS:-10}" \
    DURATION="${ML_FAULT_DURATION:-2m}"
fi

METRIC_FILES=()
while IFS= read -r metrics_file; do
  METRIC_FILES+=("${metrics_file}")
done < <(find "${RESULTS_BASE}" -path '*/reports/trial_*_metrics.json' -type f | sort)
if [ "${#METRIC_FILES[@]}" -gt 0 ]; then
  python3 evaluation/summarise_trial.py \
    --aggregate "${METRIC_FILES[@]}" \
    --output "${RESULTS_BASE}/run_summary.json"
fi

echo ""
echo "=============================================="
echo "  Research Smoke Matrix Complete"
echo "  Results: ${RESULTS_BASE}"
echo "  Summary: ${RESULTS_BASE}/run_summary.json"
echo "=============================================="

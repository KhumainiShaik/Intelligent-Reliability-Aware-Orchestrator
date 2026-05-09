#!/usr/bin/env bash
set -euo pipefail

# 08_deploy_system.sh — deploy the system to GKE with direct Helm commands.
#
# Requires image overrides (from 07_build_and_push_images.sh):
#   CTRL_IMAGE_REPO, CTRL_IMAGE_TAG, WORK_IMAGE_REPO, WORK_IMAGE_TAG
#
# Optional env:
#   INSTALL_PREREQS=1          # install ingress-nginx, Argo CD, Argo Rollouts, Litmus
#   INSTALL_MONITORING=1        # set 0 to skip monitoring chart install
#   APPLY_POLICY_CONFIGMAP=1    # set 0 to skip rl-policy-artifact ConfigMap
#   POLICY_ARTIFACT_FILE=path   # override artifact path (default: auto-detect)
#   WORKLOAD_CHART=charts/workload
#   WORKLOAD_RELEASE=workload
#   WORKLOAD_INGRESS_HOST=workload.local
#
# Usage:
#   CTRL_IMAGE_REPO=... CTRL_IMAGE_TAG=... WORK_IMAGE_REPO=... WORK_IMAGE_TAG=... ./scripts/gke/08_deploy_system.sh

CTRL_IMAGE_REPO="${CTRL_IMAGE_REPO:-}"
CTRL_IMAGE_TAG="${CTRL_IMAGE_TAG:-}"
WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"
WORK_IMAGE_TAG="${WORK_IMAGE_TAG:-}"

INSTALL_PREREQS="${INSTALL_PREREQS:-1}"
INSTALL_MONITORING="${INSTALL_MONITORING:-1}"
APPLY_POLICY_CONFIGMAP="${APPLY_POLICY_CONFIGMAP:-1}"
POLICY_ARTIFACT_FILE="${POLICY_ARTIFACT_FILE:-}"
WORKLOAD_CHART="${WORKLOAD_CHART:-charts/workload}"
WORKLOAD_RELEASE="${WORKLOAD_RELEASE:-workload}"
WORKLOAD_INGRESS_HOST="${WORKLOAD_INGRESS_HOST:-}"

if [ -z "${CTRL_IMAGE_REPO}" ] || [ -z "${CTRL_IMAGE_TAG}" ] || [ -z "${WORK_IMAGE_REPO}" ] || [ -z "${WORK_IMAGE_TAG}" ]; then
  echo "ERROR: Set CTRL_IMAGE_REPO/CTRL_IMAGE_TAG and WORK_IMAGE_REPO/WORK_IMAGE_TAG" >&2
  exit 2
fi

if [ "${INSTALL_PREREQS}" = "1" ]; then
  echo "Deploying cluster prerequisites..."
  ./scripts/gke/06_deploy_prereqs.sh
fi

if [ "${INSTALL_MONITORING}" = "1" ]; then
  echo "Deploying monitoring stack..."
  helm upgrade --install monitoring charts/monitoring \
    --namespace monitoring --create-namespace

  # Best-effort: monitoring might already be ready.
  kubectl wait -n monitoring --for=condition=ready pod --all --timeout=300s || true
fi

echo "Deploying CRDs..."
make install-crd

if [ "${APPLY_POLICY_CONFIGMAP}" = "1" ]; then
  echo "Applying RL policy ConfigMap (rl-policy-artifact)..."
  if [ -n "${POLICY_ARTIFACT_FILE}" ]; then
    ./scripts/05_setup_policy_artifact.sh "${POLICY_ARTIFACT_FILE}"
  else
    ./scripts/05_setup_policy_artifact.sh
  fi
fi

echo "Deploying controller (image override)..."
helm upgrade --install controller charts/controller \
  --namespace orchestrated-rollout --create-namespace \
  --set image.repository="${CTRL_IMAGE_REPO}" \
  --set image.tag="${CTRL_IMAGE_TAG}" \
  --set image.pullPolicy=IfNotPresent \
  --wait --timeout 10m

echo "Deploying workload (image override)..."
workload_set_args=(
  --set image.repository="${WORK_IMAGE_REPO}"
  --set image.tag="${WORK_IMAGE_TAG}"
  --set image.pullPolicy=IfNotPresent
)
if [ -n "${WORKLOAD_INGRESS_HOST}" ]; then
  workload_set_args+=(--set ingress.host="${WORKLOAD_INGRESS_HOST}")
fi

helm upgrade --install "${WORKLOAD_RELEASE}" "${WORKLOAD_CHART}" \
  --namespace orchestrated-rollout --create-namespace \
  "${workload_set_args[@]}" \
  --wait --timeout 10m

echo "Waiting for orchestrated-rollout pods..."
kubectl wait -n orchestrated-rollout --for=condition=ready pod --all --timeout=300s

echo "Deploy complete. Next:"
echo "  ./scripts/trigger_rollout.sh v2.0.0"
echo "  (Ingress IP) kubectl get svc -n ingress-nginx ingress-nginx-controller -o wide"

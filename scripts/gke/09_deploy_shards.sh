#!/usr/bin/env bash
set -euo pipefail

# 09_deploy_shards.sh — deploy N shard namespaces on ONE GKE cluster.
#
# Creates N namespaces, installs controller+workload per namespace, configures a unique ingress host,
# applies the RL policy ConfigMap, and installs Litmus generic experiments + RBAC for chaos injection.
#
# Required env:
#   CTRL_IMAGE_REPO, CTRL_IMAGE_TAG, WORK_IMAGE_REPO, WORK_IMAGE_TAG
#
# Optional env:
#   SHARD_COUNT (default 9)
#   NAMESPACE_PREFIX (default orchestrated-rollout-s)
#   INGRESS_HOST_PREFIX (default workload-s)
#   POLICY_ARTIFACT_FILE (default artifacts/v11_no_forecast/policy_artifact.json)
#   CHAOS_SERVICEACCOUNT (default oroll-litmus-admin)
#   CHAOS_CLUSTERROLE (default oroll-litmus-admin)
#
# Example:
#   CTRL_IMAGE_REPO=... CTRL_IMAGE_TAG=... WORK_IMAGE_REPO=... WORK_IMAGE_TAG=... \
#   ./scripts/gke/09_deploy_shards.sh

SHARD_COUNT="${SHARD_COUNT:-9}"
NAMESPACE_PREFIX="${NAMESPACE_PREFIX:-orchestrated-rollout-s}"
INGRESS_HOST_PREFIX="${INGRESS_HOST_PREFIX:-workload-s}"
POLICY_ARTIFACT_FILE="${POLICY_ARTIFACT_FILE:-artifacts/v11_no_forecast/policy_artifact.json}"

CHAOS_SERVICEACCOUNT="${CHAOS_SERVICEACCOUNT:-oroll-litmus-admin}"
CHAOS_CLUSTERROLE="${CHAOS_CLUSTERROLE:-oroll-litmus-admin}"

CTRL_IMAGE_REPO="${CTRL_IMAGE_REPO:-}"
CTRL_IMAGE_TAG="${CTRL_IMAGE_TAG:-}"
WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"
WORK_IMAGE_TAG="${WORK_IMAGE_TAG:-}"

if [ -z "${CTRL_IMAGE_REPO}" ] || [ -z "${CTRL_IMAGE_TAG}" ] || [ -z "${WORK_IMAGE_REPO}" ] || [ -z "${WORK_IMAGE_TAG}" ]; then
  echo "ERROR: Set CTRL_IMAGE_REPO/CTRL_IMAGE_TAG and WORK_IMAGE_REPO/WORK_IMAGE_TAG" >&2
  exit 2
fi

if ! [[ "${SHARD_COUNT}" =~ ^[0-9]+$ ]] || [ "${SHARD_COUNT}" -lt 1 ]; then
  echo "ERROR: SHARD_COUNT must be an integer >= 1" >&2
  exit 2
fi

if [ ! -f "${POLICY_ARTIFACT_FILE}" ]; then
  echo "ERROR: POLICY_ARTIFACT_FILE not found: ${POLICY_ARTIFACT_FILE}" >&2
  exit 2
fi

echo "Deploying ${SHARD_COUNT} shard namespaces"
echo "  Controller image: ${CTRL_IMAGE_REPO}:${CTRL_IMAGE_TAG}"
echo "  Workload image:   ${WORK_IMAGE_REPO}:${WORK_IMAGE_TAG}"
echo "  Policy artifact:  ${POLICY_ARTIFACT_FILE}"

# Ensure ClusterRole for chaos exists once.
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

for i in $(seq 0 $((SHARD_COUNT - 1))); do
  ns="${NAMESPACE_PREFIX}${i}"
  host="${INGRESS_HOST_PREFIX}${i}.local"
  ctrl_release="controller-s${i}"

  echo ""
  echo "=== Shard ${i}/${SHARD_COUNT}: ns=${ns} host=${host} controllerRelease=${ctrl_release} ==="

  kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f -

  # Install Litmus generic experiments into this namespace (ChaosExperiment is namespaced).
  kubectl apply -f "k8s/chaos/generic-experiments.yaml" -n "${ns}" >/dev/null || true

  # Create per-namespace SA + binding for chaos.
  kubectl create serviceaccount "${CHAOS_SERVICEACCOUNT}" -n "${ns}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${CHAOS_CLUSTERROLE}-${ns}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${CHAOS_CLUSTERROLE}
subjects:
  - kind: ServiceAccount
    name: ${CHAOS_SERVICEACCOUNT}
    namespace: ${ns}
EOF

  # Policy ConfigMap (required for RL mode).
  kubectl create configmap rl-policy-artifact -n "${ns}" \
    --from-file=policy_artifact.json="${POLICY_ARTIFACT_FILE}" \
    --dry-run=client -o yaml | kubectl apply -f -

  # Controller: unique release name to avoid ClusterRole collisions.
  helm upgrade --install "${ctrl_release}" charts/controller \
    --namespace "${ns}" --create-namespace \
    --set image.repository="${CTRL_IMAGE_REPO}" \
    --set image.tag="${CTRL_IMAGE_TAG}" \
    --set image.pullPolicy=IfNotPresent \
    --wait --timeout 10m

  # Workload: keep release name constant per namespace; set unique ingress host.
  helm upgrade --install workload charts/workload \
    --namespace "${ns}" --create-namespace \
    --set image.repository="${WORK_IMAGE_REPO}" \
    --set image.tag="${WORK_IMAGE_TAG}" \
    --set image.pullPolicy=IfNotPresent \
    --set ingress.host="${host}" \
    --wait --timeout 10m

done

echo ""
echo "All shard namespaces deployed."

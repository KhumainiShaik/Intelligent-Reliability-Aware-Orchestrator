#!/usr/bin/env bash
# 05_setup_policy_artifact.sh — Create/update the rl-policy-artifact ConfigMap.
set -euo pipefail

NAMESPACE="${NAMESPACE:-orchestrated-rollout}"
CONFIGMAP_NAME="${CONFIGMAP_NAME:-rl-policy-artifact}"

ARTIFACT_FILE="${1:-}"
if [ -z "${ARTIFACT_FILE}" ]; then
    for candidate in \
        artifacts/v11_no_forecast/policy_artifact.json \
        artifacts/v10b/policy_artifact.json \
        artifacts/v8_no_forecast/policy_artifact.json \
        artifacts/v8/policy_artifact.json \
        artifacts/v1/policy_artifact.json
    do
        if [ -f "${candidate}" ]; then
            ARTIFACT_FILE="${candidate}"
            break
        fi
    done
fi

if [ -z "${ARTIFACT_FILE}" ] || [ ! -f "${ARTIFACT_FILE}" ]; then
    echo "ERROR: policy_artifact.json not found. Provide a path as arg1, e.g.:"
    echo "  ./scripts/05_setup_policy_artifact.sh artifacts/v11_no_forecast/policy_artifact.json"
    exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
    echo "ERROR: kubectl is required but not installed."
    exit 1
fi

kubectl get ns "${NAMESPACE}" >/dev/null 2>&1 || {
    echo "ERROR: Namespace ${NAMESPACE} not found. Create it first (e.g. via ./scripts/01_setup_cluster.sh)."
    exit 1
}

echo "Creating/updating ConfigMap ${CONFIGMAP_NAME} in namespace ${NAMESPACE}"
echo "  from: ${ARTIFACT_FILE}"

kubectl create configmap "${CONFIGMAP_NAME}" -n "${NAMESPACE}" \
    --from-file=policy_artifact.json="${ARTIFACT_FILE}" \
    --dry-run=client -o yaml \
    | kubectl apply -f -

echo "OK: ConfigMap applied. Restart the controller to reload the policy, e.g.:"
echo "  kubectl rollout restart deploy -n ${NAMESPACE} -l app.kubernetes.io/name=controller"

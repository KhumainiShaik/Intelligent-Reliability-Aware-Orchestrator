#!/usr/bin/env bash
set -euo pipefail

# Creates or updates the fixed GitLab CI/CD variables used by the production
# GitOps demo pipeline. Run locally from the aks85 checkout.
#
# Required:
#   GITLAB_TOKEN                 Personal/project access token with Maintainer
#                                access and API scope.
#
# Optional:
#   GITLAB_API_URL               Default: https://campus.cs.le.ac.uk/api/v4
#   GITLAB_PROJECT_ID            Default: pgt_project/25_26_spring/aks85
#                                A numeric ID or URL-encoded/full path.
#   GCP_SERVICE_ACCOUNT_KEY_FILE Path to service account JSON key.
#   GCP_SERVICE_ACCOUNT_KEY_B64  Base64 service account JSON. Used when file is
#                                not supplied.

GITLAB_API_URL="${GITLAB_API_URL:-https://campus.cs.le.ac.uk/api/v4}"
GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-pgt_project/25_26_spring/aks85}"
GITLAB_TOKEN="${GITLAB_TOKEN:?GITLAB_TOKEN is required}"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-level-slate-494713-u5}"
GCP_REGION="${GCP_REGION:-europe-west2}"
GKE_CLUSTER_NAME="${GKE_CLUSTER_NAME:-orchestrated-rollout}"
AR_REPO="${AR_REPO:-oroll}"
WORKLOAD_APP_NAME="${WORKLOAD_APP_NAME:-workload-cpu-bound-fastapi}"
WORKLOAD_NAMESPACE="${WORKLOAD_NAMESPACE:-workload-cpu}"
ORCHESTRATED_ROLLOUT_NAME="${ORCHESTRATED_ROLLOUT_NAME:-cpu-bound-fastapi-portability-template}"

urlencode() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
}

project_ref="$(urlencode "${GITLAB_PROJECT_ID}")"

if [ -n "${GCP_SERVICE_ACCOUNT_KEY_FILE:-}" ]; then
  if [ ! -f "${GCP_SERVICE_ACCOUNT_KEY_FILE}" ]; then
    echo "ERROR: ${GCP_SERVICE_ACCOUNT_KEY_FILE} not found" >&2
    exit 1
  fi
  GCP_SERVICE_ACCOUNT_KEY_B64="$(base64 < "${GCP_SERVICE_ACCOUNT_KEY_FILE}" | tr -d '\n')"
elif [ -z "${GCP_SERVICE_ACCOUNT_KEY_B64:-}" ]; then
  echo "ERROR: set GCP_SERVICE_ACCOUNT_KEY_FILE or GCP_SERVICE_ACCOUNT_KEY_B64" >&2
  exit 1
fi

put_variable() {
  local key="$1"
  local value="$2"
  local masked="${3:-false}"
  local endpoint="${GITLAB_API_URL}/projects/${project_ref}/variables/${key}"

  if curl --silent --show-error --fail \
    --header "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
    "${endpoint}" >/dev/null; then
    curl --silent --show-error --fail --request PUT \
      --header "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
      --form "value=${value}" \
      --form "protected=false" \
      --form "masked=${masked}" \
      --form "raw=true" \
      --form "variable_type=env_var" \
      "${endpoint}" >/dev/null
    echo "updated ${key}"
  else
    curl --silent --show-error --fail --request POST \
      --header "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
      --form "key=${key}" \
      --form "value=${value}" \
      --form "protected=false" \
      --form "masked=${masked}" \
      --form "raw=true" \
      --form "variable_type=env_var" \
      "${GITLAB_API_URL}/projects/${project_ref}/variables" >/dev/null
    echo "created ${key}"
  fi
}

put_variable "GCP_SERVICE_ACCOUNT_KEY_B64" "${GCP_SERVICE_ACCOUNT_KEY_B64}" "false"
put_variable "GCP_PROJECT_ID" "${GCP_PROJECT_ID}"
put_variable "GCP_REGION" "${GCP_REGION}"
put_variable "GKE_CLUSTER_NAME" "${GKE_CLUSTER_NAME}"
put_variable "AR_REPO" "${AR_REPO}"
put_variable "WORKLOAD_APP_NAME" "${WORKLOAD_APP_NAME}"
put_variable "WORKLOAD_NAMESPACE" "${WORKLOAD_NAMESPACE}"
put_variable "ORCHESTRATED_ROLLOUT_NAME" "${ORCHESTRATED_ROLLOUT_NAME}"

cat <<EOF

GitLab CI/CD variables are configured for ${GITLAB_PROJECT_ID}.
The production pipeline can now authenticate to Artifact Registry and GKE.
EOF

#!/usr/bin/env bash
set -euo pipefail

# 01_auth_and_project.sh — authenticate (interactive) and select/create a project.
#
# Usage:
#   GCP_ACCOUNT_EMAIL="you@gmail.com" PROJECT_ID="my-project" ./scripts/gke/01_auth_and_project.sh
#
# Notes:
# - This script will open a browser flow for authentication.
# - GKE requires billing to be enabled on the selected project.

GCP_ACCOUNT_EMAIL="${GCP_ACCOUNT_EMAIL:-}"
PROJECT_ID="${PROJECT_ID:-}"

if [ -z "${GCP_ACCOUNT_EMAIL}" ]; then
  echo "ERROR: set GCP_ACCOUNT_EMAIL" >&2
  exit 2
fi
if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: set PROJECT_ID" >&2
  exit 2
fi

echo "Authenticating ${GCP_ACCOUNT_EMAIL} (interactive)..."
# --update-adc ensures Application Default Credentials are also set.
gcloud auth login "${GCP_ACCOUNT_EMAIL}" --update-adc

gcloud config set account "${GCP_ACCOUNT_EMAIL}"

echo "Checking if project exists: ${PROJECT_ID}"
if gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Project exists: ${PROJECT_ID}"
else
  echo "Creating project: ${PROJECT_ID}"
  gcloud projects create "${PROJECT_ID}" --name="orchestrated-rollout-dissertation" || {
    echo "ERROR: could not create project. If it already exists but you lack permission, use an existing project." >&2
    exit 1
  }
fi

gcloud config set project "${PROJECT_ID}"

cat <<EOF

Next steps:
1) Enable billing for this project (Console → Billing) if not already enabled.
2) Run: ./scripts/gke/02_enable_apis.sh
EOF

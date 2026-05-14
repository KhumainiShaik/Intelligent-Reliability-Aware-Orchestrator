#!/usr/bin/env bash
set -euo pipefail

# Creates/updates the GitHub Actions secret needed by the GitOps demo workflow.
# Requires GitHub CLI authentication: gh auth login
#
# Required:
#   GITHUB_REPOSITORY             owner/repo, for example khumaini/aks85
#   GCP_SERVICE_ACCOUNT_KEY_FILE  path to service account JSON key

GITHUB_REPOSITORY="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required, e.g. owner/repo}"
GCP_SERVICE_ACCOUNT_KEY_FILE="${GCP_SERVICE_ACCOUNT_KEY_FILE:?GCP_SERVICE_ACCOUNT_KEY_FILE is required}"

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: GitHub CLI is required. Install it and run: gh auth login" >&2
  exit 1
fi

if [ ! -f "${GCP_SERVICE_ACCOUNT_KEY_FILE}" ]; then
  echo "ERROR: ${GCP_SERVICE_ACCOUNT_KEY_FILE} not found" >&2
  exit 1
fi

GCP_SERVICE_ACCOUNT_KEY_B64="$(base64 < "${GCP_SERVICE_ACCOUNT_KEY_FILE}" | tr -d '\n')"

printf '%s' "${GCP_SERVICE_ACCOUNT_KEY_B64}" | gh secret set GCP_SERVICE_ACCOUNT_KEY_B64 \
  --repo "${GITHUB_REPOSITORY}" \
  --body-file -

echo "GitHub Actions secret configured: GCP_SERVICE_ACCOUNT_KEY_B64"

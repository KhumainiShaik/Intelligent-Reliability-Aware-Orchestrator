#!/usr/bin/env bash
set -euo pipefail

# 03_artifact_registry.sh — create a Docker Artifact Registry repo and configure auth.
#
# Usage:
#   PROJECT_ID="my-project" REGION="europe-west2" AR_REPO="oroll" ./scripts/gke/03_artifact_registry.sh

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west2}"
AR_REPO="${AR_REPO:-oroll}"

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "Ensuring Artifact Registry repo exists: ${AR_REPO} (${REGION})"
if gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1; then
  echo "Artifact Registry repo already exists."
else
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Orchestrated Rollout images"
fi

echo "Configuring Docker auth for Artifact Registry..."
# This modifies ~/.docker/config.json for the domain.
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "Done. Next: ./scripts/gke/04_create_cluster.sh"

#!/usr/bin/env bash
set -euo pipefail

# 02_enable_apis.sh — enable required Google Cloud APIs.
#
# Usage:
#   PROJECT_ID="my-project" ./scripts/gke/02_enable_apis.sh

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "Enabling APIs for project: ${PROJECT_ID}"

gcloud services enable \
  compute.googleapis.com \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  storage-api.googleapis.com \
  storage-component.googleapis.com

echo "APIs enabled. Next: ./scripts/gke/03_artifact_registry.sh"

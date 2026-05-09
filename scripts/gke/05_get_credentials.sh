#!/usr/bin/env bash
set -euo pipefail

# 05_get_credentials.sh — configure kubectl context for the cluster.
#
# Usage:
#   PROJECT_ID="my-project" REGION="europe-west2" CLUSTER_NAME="oroll" ./scripts/gke/05_get_credentials.sh

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west2}"
CLUSTER_NAME="${CLUSTER_NAME:-orchestrated-rollout}"

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

gcloud container clusters get-credentials "${CLUSTER_NAME}" --region "${REGION}"

echo "kubectl context set. Current context:"
kubectl config current-context

echo "Next: ./scripts/gke/06_deploy_prereqs.sh"

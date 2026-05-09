#!/usr/bin/env bash
set -euo pipefail

# 04_create_cluster.sh — create a STANDARD (non-Autopilot) GKE cluster.
#
# Why standard: Autopilot can block/limit certain workloads (e.g., chaos tooling).
#
# Usage:
#   PROJECT_ID="my-project" REGION="europe-west2" CLUSTER_NAME="oroll" \
#   NODE_COUNT=2 MACHINE_TYPE="e2-standard-4" ./scripts/gke/04_create_cluster.sh

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west2}"
CLUSTER_NAME="${CLUSTER_NAME:-orchestrated-rollout}"
NODE_COUNT="${NODE_COUNT:-2}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-4}"
K8S_VERSION="${K8S_VERSION:-}"

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "Creating GKE cluster (if missing): ${CLUSTER_NAME} in ${REGION}"
echo "Node pools: ${NODE_COUNT} nodes per zone. Regional europe-west2 with NODE_COUNT=2 gives six total nodes."

# Create only if it doesn't exist.
if gcloud container clusters describe "${CLUSTER_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Cluster already exists: ${CLUSTER_NAME}"
  exit 0
fi

VERSION_FLAG=()
if [ -n "${K8S_VERSION}" ]; then
  VERSION_FLAG=(--cluster-version "${K8S_VERSION}")
fi

gcloud container clusters create "${CLUSTER_NAME}" \
  --region "${REGION}" \
  --num-nodes "${NODE_COUNT}" \
  --machine-type "${MACHINE_TYPE}" \
  --enable-ip-alias \
  --release-channel "regular" \
  --disk-size "50" \
  ${VERSION_FLAG[@]:-}

echo "Cluster created. Next: ./scripts/gke/05_get_credentials.sh"

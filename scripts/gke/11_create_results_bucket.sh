#!/usr/bin/env bash
set -euo pipefail

# 11_create_results_bucket.sh — create (or reuse) a GCS bucket for experiment artifacts.
#
# Usage:
#   PROJECT_ID="my-project" REGION="europe-west2" GCS_BUCKET="my-unique-bucket" ./scripts/gke/11_create_results_bucket.sh
#
# Notes:
# - Bucket names must be globally unique.
# - This script enables uniform bucket-level access.

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

# Prefer REGION (consistent with other scripts), fallback to LOCATION.
LOCATION="${REGION:-${LOCATION:-}}"
if [ -z "${LOCATION}" ]; then
  LOCATION="europe-west2"
fi

GCS_BUCKET="${GCS_BUCKET:-${BUCKET_NAME:-}}"
if [ -z "${GCS_BUCKET}" ]; then
  echo "ERROR: Set GCS_BUCKET (or BUCKET_NAME)" >&2
  exit 2
fi

STORAGE_CLASS="${STORAGE_CLASS:-STANDARD}"

gcloud config set project "${PROJECT_ID}" >/dev/null

BUCKET_URI="gs://${GCS_BUCKET}"

echo "Ensuring GCS bucket exists: ${BUCKET_URI}"

if gcloud storage buckets describe "${BUCKET_URI}" >/dev/null 2>&1; then
  echo "Bucket already exists."
  exit 0
fi

echo "Creating bucket in ${LOCATION} (class=${STORAGE_CLASS})..."

gcloud storage buckets create "${BUCKET_URI}" \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --default-storage-class="${STORAGE_CLASS}" \
  --uniform-bucket-level-access

echo "Bucket created: ${BUCKET_URI}"

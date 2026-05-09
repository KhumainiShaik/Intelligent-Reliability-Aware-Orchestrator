#!/usr/bin/env bash
set -euo pipefail

# download_min.sh — download a grid/experiment directory from GCS, excluding huge raw k6 outputs by default.
#
# Usage:
#   GCS_RESULTS_URI=gs://my-bucket/prefix bash ./scripts/gcs/download_min.sh <remote_relative_path> [local_dest]
#
# Example:
#   GCS_RESULTS_URI=gs://my-bucket/oroll-results \
#     bash ./scripts/gcs/download_min.sh experiments/grid_20260403_112530_rl_shard0-of-9

usage() {
  echo "Usage: bash ./scripts/gcs/download_min.sh <remote_relative_path> [local_dest]" >&2
  echo "  Requires either: GCS_RESULTS_URI=gs://bucket/prefix OR GCS_BUCKET (+ optional GCS_PREFIX)" >&2
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

REMOTE_REL="${1:-}"
LOCAL_DEST="${2:-${REMOTE_REL}}"

if [ -z "${REMOTE_REL}" ]; then
  usage
  exit 2
fi

if ! command -v gsutil >/dev/null 2>&1; then
  echo "ERROR: gsutil is required but not found on PATH" >&2
  exit 2
fi

# On macOS, gsutil multiprocessing can hang (Python issue33725).
# We keep multithreading (-m) but force a single process.
GSUTIL_OPTS=()
if [ "$(uname -s)" = "Darwin" ]; then
  GSUTIL_OPTS+=( -o "GSUtil:parallel_process_count=1" )
fi

BASE_URI=""
if [ -n "${GCS_RESULTS_URI:-}" ]; then
  BASE_URI="${GCS_RESULTS_URI%/}"
else
  if [ -z "${GCS_BUCKET:-}" ]; then
    echo "ERROR: Set GCS_RESULTS_URI or GCS_BUCKET" >&2
    exit 2
  fi
  prefix="${GCS_PREFIX:-}"
  prefix="${prefix#/}"
  prefix="${prefix%/}"
  if [ -n "${prefix}" ]; then
    BASE_URI="gs://${GCS_BUCKET}/${prefix}"
  else
    BASE_URI="gs://${GCS_BUCKET}"
  fi
fi

REMOTE_REL="${REMOTE_REL#/}"
REMOTE_URI="${BASE_URI}/${REMOTE_REL}"

# Default: exclude raw k6 JSON (trial_<n>.json). Override by setting GCS_DOWNLOAD_EXCLUDE_REGEX to empty.
EXCLUDE_REGEX_DEFAULT='.*/k6_results/trial_[0-9]+\\.json$'
EXCLUDE_REGEX="${GCS_DOWNLOAD_EXCLUDE_REGEX:-${EXCLUDE_REGEX_DEFAULT}}"

mkdir -p "$(dirname "${LOCAL_DEST}")"

echo "[gcs] rsync -r '${REMOTE_URI}' -> '${LOCAL_DEST}' (exclude='${EXCLUDE_REGEX}')"
if [ -n "${EXCLUDE_REGEX}" ]; then
  gsutil "${GSUTIL_OPTS[@]}" -m rsync -r -x "${EXCLUDE_REGEX}" "${REMOTE_URI}" "${LOCAL_DEST}"
else
  gsutil "${GSUTIL_OPTS[@]}" -m rsync -r "${REMOTE_URI}" "${LOCAL_DEST}"
fi

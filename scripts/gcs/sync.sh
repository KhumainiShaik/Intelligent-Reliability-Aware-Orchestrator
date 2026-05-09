#!/usr/bin/env bash
set -euo pipefail

# sync.sh — sync a local file/dir into a GCS results prefix.
#
# Usage:
#   GCS_RESULTS_URI=gs://my-bucket/prefix bash ./scripts/gcs/sync.sh <local_path>
#   # or
#   GCS_BUCKET=my-bucket GCS_PREFIX=prefix bash ./scripts/gcs/sync.sh <local_path>
#
# Notes:
# - For directories, uses: gsutil -m rsync -r
# - For files, uses:       gsutil -m cp
# - Destination is: <base>/<relative_path>

usage() {
  echo "Usage: bash ./scripts/gcs/sync.sh <local_path> [remote_relative_path]" >&2
  echo "  Requires either: GCS_RESULTS_URI=gs://bucket/prefix OR GCS_BUCKET (+ optional GCS_PREFIX)" >&2
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

LOCAL_PATH="${1:-}"
REMOTE_REL_ARG="${2:-}"

if [ -z "${LOCAL_PATH}" ]; then
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

# Derive a stable remote relative path.
LOCAL_REL="${LOCAL_PATH}"
if [[ "${LOCAL_REL}" = /* ]]; then
  if [[ "${LOCAL_REL}" = "${PWD%/}/"* ]]; then
    LOCAL_REL="${LOCAL_REL#"${PWD%/}/"}"
  else
    LOCAL_REL="$(basename "${LOCAL_REL}")"
  fi
fi

REMOTE_REL="${REMOTE_REL_ARG:-${LOCAL_REL}}"
REMOTE_REL="${REMOTE_REL#/}"

DEST_URI="${BASE_URI}/${REMOTE_REL}"

if [ ! -e "${LOCAL_PATH}" ]; then
  echo "ERROR: local path not found: ${LOCAL_PATH}" >&2
  exit 2
fi

EXCLUDE_REGEX="${GCS_EXCLUDE_REGEX:-}"

if [ -d "${LOCAL_PATH}" ]; then
  echo "[gcs] rsync -r '${LOCAL_PATH}' -> '${DEST_URI}'"
  if [ -n "${EXCLUDE_REGEX}" ]; then
    gsutil "${GSUTIL_OPTS[@]}" -m rsync -r -x "${EXCLUDE_REGEX}" "${LOCAL_PATH}" "${DEST_URI}"
  else
    gsutil "${GSUTIL_OPTS[@]}" -m rsync -r "${LOCAL_PATH}" "${DEST_URI}"
  fi
else
  echo "[gcs] cp '${LOCAL_PATH}' -> '${DEST_URI}'"
  gsutil "${GSUTIL_OPTS[@]}" -m cp "${LOCAL_PATH}" "${DEST_URI}"
fi

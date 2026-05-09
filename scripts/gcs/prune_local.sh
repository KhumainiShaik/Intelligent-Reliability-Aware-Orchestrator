#!/usr/bin/env bash
set -euo pipefail

# prune_local.sh — remove large local artifacts after they've been uploaded.
#
# Default behaviour is conservative: delete only k6 raw JSON outputs
# (k6_results/trial_*.json), keeping episodes + k6 stdout + summaries.

usage() {
  echo "Usage: bash ./scripts/gcs/prune_local.sh <path>" >&2
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

TARGET_PATH="${1:-}"
if [ -z "${TARGET_PATH}" ]; then
  usage
  exit 2
fi

if [ ! -d "${TARGET_PATH}" ]; then
  echo "ERROR: not a directory: ${TARGET_PATH}" >&2
  exit 2
fi

# k6 raw JSON can be extremely large and is not used by evaluation/analyse.py.
# Keep:
# - k6_results/trial_*_stdout.txt
# - k6_results/trial_*_summary.json
# - episodes/*.json

deleted=0
while IFS= read -r -d '' f; do
  rm -f "${f}"
  deleted=$((deleted + 1))
done < <(
  find "${TARGET_PATH}" -type f \
    -regextype posix-extended \
    -regex '.*/k6_results/trial_[0-9]+\.json' \
    -print0 2>/dev/null || true
)

echo "[prune] deleted ${deleted} file(s) under ${TARGET_PATH}"

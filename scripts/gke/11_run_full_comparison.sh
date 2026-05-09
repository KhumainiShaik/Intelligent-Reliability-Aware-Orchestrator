#!/usr/bin/env bash
set -euo pipefail

# 11_run_full_comparison.sh — Run complete comparison: RL + 4 baselines
#
# Runs 5 sequential grid executions on the same shard cluster:
#   1. RL policy — full action set (controller selects)
#   2. baseline-rolling  — ACTION_SET=rolling
#   3. baseline-canary   — ACTION_SET=canary
#   4. baseline-delay    — ACTION_SET=delay
#   5. baseline-prescale — ACTION_SET=pre-scale
#
# Each run uses the same GCS bucket/prefix so all results land together.
# Results directory: experiments/comparison_<TIMESTAMP>/
#
# Required env: WORK_IMAGE_REPO
# Optional env: SHARD_COUNT, REPEATS, GCS_BUCKET, etc.

COMPARISON_TIMESTAMP="${COMPARISON_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
SHARD_COUNT="${SHARD_COUNT:-9}"
REPEATS="${REPEATS:-5}"

# The modes to run: "rl" uses the full action set, baselines constrain to one.
MODES=("rl" "rolling" "canary" "delay" "pre-scale")

echo "=============================================="
echo "  Full Comparison Run"
echo "  Timestamp:  ${COMPARISON_TIMESTAMP}"
echo "  Shards:     ${SHARD_COUNT}"
echo "  Repeats:    ${REPEATS}"
echo "  Modes:      ${MODES[*]}"
echo "  Total grid runs: ${#MODES[@]}"
echo "=============================================="

for mode in "${MODES[@]}"; do
  echo ""
  echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
  echo "  Starting mode: ${mode}"
  echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"

  if [ "${mode}" = "rl" ]; then
    export ACTION_SET=""
    grid_ts="${COMPARISON_TIMESTAMP}_rl"
  else
    export ACTION_SET="${mode}"
    grid_ts="${COMPARISON_TIMESTAMP}_baseline-${mode}"
  fi

  export GRID_TIMESTAMP="${grid_ts}"
  export SHARD_COUNT="${SHARD_COUNT}"
  export REPEATS="${REPEATS}"

  # Inherit all other env vars (GCS_BUCKET, WORK_IMAGE_REPO, etc.)
  ./scripts/gke/10_run_grid_shards.sh

  echo ""
  echo "<<< Mode ${mode} complete >>>"
  echo ""
done

echo "=============================================="
echo "  Full Comparison Complete"
echo "  Timestamp: ${COMPARISON_TIMESTAMP}"
echo "  Download with:"
echo "    bash scripts/gcs/download_min.sh"
echo "  Analyse with:"
echo "    python3 evaluation/analyse.py results/comparison_${COMPARISON_TIMESTAMP}/ ..."
echo "=============================================="

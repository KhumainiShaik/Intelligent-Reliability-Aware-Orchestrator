#!/usr/bin/env bash
set -euo pipefail

# Patch script: re-run steady_* combos that failed due to catch{} syntax error in steady.js.
# Run AFTER current baselines complete (both phases).
# Groups modes by namespace to parallelise:
#   Phase A: RL (s0-s2)
#   Phase B: rolling (s0-s2) + canary (s5-s7)    — non-overlapping namespaces
#   Phase C: delay (s0-s2) + pre-scale (s5-s7)   — non-overlapping namespaces

TIMESTAMP="${GRID_TIMESTAMP:-20260406_140509}"
export GCS_RESULTS_URI="${GCS_RESULTS_URI:-gs://artifacts-orchastratorcrd/oroll-results/comparison_${TIMESTAMP}}"
export GRID_TIMESTAMP="${TIMESTAMP}"
export SLO_P95_LATENCY_MS="${SLO_P95_LATENCY_MS:-500}"
export K6_SAVE_SUMMARY_JSON=1

echo "==== STEADY PATCH RUN ===="
echo "Refreshing auth..."
gcloud container clusters get-credentials orchestrated-rollout --region europe-west2 --quiet

# Clean poisoned steady directories
echo "Cleaning poisoned steady combo directories..."
for d in experiments/grid_${TIMESTAMP}_*_shard*/steady_*/; do
    if [ -d "$d" ]; then
        echo "  rm -rf $d"
        rm -rf "$d"
    fi
done

run_steady_patch() {
    local MODE="$1"
    local SHARD_COUNT="$2"
    local NS_OFFSET="$3"   # offset into s0..s8 namespace pool

    echo ""
    echo "==== Patching ${MODE} steady combos (shards 0-2, ns offset=${NS_OFFSET}) ===="

    for SHARD_IDX in 0 1 2; do
        local NS="orchestrated-rollout-s$((NS_OFFSET + SHARD_IDX))"
        echo "  Shard ${SHARD_IDX}: namespace=${NS}"

        POLICY_MODE="${MODE}" \
        SHARD_COUNT="${SHARD_COUNT}" \
        SHARD_INDEX="${SHARD_IDX}" \
        GRID_TIMESTAMP="${TIMESTAMP}" \
        GRID_DIR="experiments/grid_${TIMESTAMP}_${MODE}_shard${SHARD_IDX}-of-${SHARD_COUNT}" \
        NAMESPACE="${NS}" \
        SCENARIOS_CSV="steady" \
        K6_SAVE_SUMMARY_JSON=1 \
        SLO_P95_LATENCY_MS="${SLO_P95_LATENCY_MS}" \
        ./scripts/full_experiment_grid.sh &
    done
    wait
    echo "  ${MODE} steady patch done."
}

reset_shards() {
    echo "Resetting shard deployments..."
    for i in $(seq 0 8); do
        kubectl rollout restart deployment/ml-workload -n "orchestrated-rollout-s${i}" 2>/dev/null || true
    done
    sleep 30
}

# Phase A: RL only (uses s0-s2)
echo "===== PHASE A: RL ====="
reset_shards
run_steady_patch "rl" 9 0

# Phase B: rolling (s0-s2) + canary (s5-s7) in parallel
echo "===== PHASE B: rolling + canary ====="
reset_shards
run_steady_patch "baseline-rolling" 5 0 &
run_steady_patch "baseline-canary" 4 5 &
wait

# Phase C: delay (s0-s2) + pre-scale (s5-s7) in parallel
echo "===== PHASE C: delay + pre-scale ====="
reset_shards
run_steady_patch "baseline-delay" 5 0 &
run_steady_patch "baseline-pre-scale" 4 5 &
wait

echo ""
echo "==== STEADY PATCH COMPLETE ===="
echo "Uploading patched results to GCS..."
for d in experiments/grid_${TIMESTAMP}_*_shard*/steady_*/; do
    if [ -d "$d" ]; then
        PARENT="$(dirname "$d")"
        gsutil -m rsync -r "$d" "${GCS_RESULTS_URI}/${PARENT}/$(basename "$d")/" || true
    fi
done
echo "All done!"

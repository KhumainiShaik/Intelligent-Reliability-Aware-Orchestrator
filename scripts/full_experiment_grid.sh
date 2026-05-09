#!/usr/bin/env bash
# full_experiment_grid.sh — Run the complete experiment grid
set -euo pipefail

REPEATS="${REPEATS:-5}"

# Optional sharding to reduce wall-clock time WITHOUT reducing coverage.
#
# Example (3-way shard):
#   SHARD_COUNT=3 SHARD_INDEX=0 ./scripts/full_experiment_grid.sh
#   SHARD_COUNT=3 SHARD_INDEX=1 ./scripts/full_experiment_grid.sh
#   SHARD_COUNT=3 SHARD_INDEX=2 ./scripts/full_experiment_grid.sh
#
# Each shard runs a disjoint subset of (scenario,fault) combinations.
SHARD_COUNT="${SHARD_COUNT:-1}"
SHARD_INDEX="${SHARD_INDEX:-0}"

if ! [[ "${SHARD_COUNT}" =~ ^[0-9]+$ ]] || [ "${SHARD_COUNT}" -lt 1 ]; then
    echo "ERROR: SHARD_COUNT must be an integer >= 1 (got: ${SHARD_COUNT})" >&2
    exit 2
fi
if ! [[ "${SHARD_INDEX}" =~ ^[0-9]+$ ]] || [ "${SHARD_INDEX}" -lt 0 ] || [ "${SHARD_INDEX}" -ge "${SHARD_COUNT}" ]; then
    echo "ERROR: SHARD_INDEX must be an integer in [0, SHARD_COUNT-1] (got: ${SHARD_INDEX})" >&2
    exit 2
fi

TIMESTAMP="${GRID_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
GRID_DIR="${GRID_DIR:-experiments/grid_${TIMESTAMP}_shard${SHARD_INDEX}-of-${SHARD_COUNT}}"

echo "=============================================="
echo "  Full Experiment Grid"
echo "  Repeats per scenario: ${REPEATS}"
echo "  Shard: ${SHARD_INDEX}/${SHARD_COUNT}"
echo "  Output: ${GRID_DIR}"
if [ -n "${GCS_RESULTS_URI:-}" ]; then
    echo "  GCS: ${GCS_RESULTS_URI} (prune_local=${GCS_PRUNE_LOCAL_AFTER_UPLOAD:-0}, delete_local=${GCS_DELETE_LOCAL_AFTER_UPLOAD:-0})"
elif [ -n "${GCS_BUCKET:-}" ]; then
    echo "  GCS: gs://${GCS_BUCKET}/${GCS_PREFIX:-} (prune_local=${GCS_PRUNE_LOCAL_AFTER_UPLOAD:-0}, delete_local=${GCS_DELETE_LOCAL_AFTER_UPLOAD:-0})"
fi
echo "=============================================="

mkdir -p "${GRID_DIR}"

SCENARIOS=("steady" "ramp" "spike")
FAULTS=("none" "pod-kill" "network-latency")

# Optional subsets (comma-separated), e.g.: SCENARIOS_CSV="ramp,spike"  FAULTS_CSV="none,pod-kill"
if [ -n "${SCENARIOS_CSV:-}" ]; then
    IFS=',' read -r -a SCENARIOS <<< "${SCENARIOS_CSV}"
fi
if [ -n "${FAULTS_CSV:-}" ]; then
    IFS=',' read -r -a FAULTS <<< "${FAULTS_CSV}"
fi

TOTAL_COMBOS=$((${#SCENARIOS[@]} * ${#FAULTS[@]}))
CURRENT=0
COMBO_INDEX=0

# Precompute how many combinations this shard will execute (for nicer progress output).
SHARD_TOTAL=0
tmp_index=0
for _scenario in "${SCENARIOS[@]}"; do
    for _fault in "${FAULTS[@]}"; do
        if [ $((tmp_index % SHARD_COUNT)) -eq "${SHARD_INDEX}" ]; then
            SHARD_TOTAL=$((SHARD_TOTAL + 1))
        fi
        tmp_index=$((tmp_index + 1))
    done
done

for scenario in "${SCENARIOS[@]}"; do
    for fault in "${FAULTS[@]}"; do
        THIS_INDEX=${COMBO_INDEX}
        COMBO_INDEX=$((COMBO_INDEX + 1))

        # Shard selection: keep only combos where (index % SHARD_COUNT) == SHARD_INDEX.
        if [ $((THIS_INDEX % SHARD_COUNT)) -ne "${SHARD_INDEX}" ]; then
            continue
        fi

        CURRENT=$((CURRENT + 1))
        EXPERIMENT_ID="grid_${scenario}_${fault}_${TIMESTAMP}"
        COMBO_DIR="${GRID_DIR}/${scenario}_${fault}"

        echo ""
        echo "=== [${CURRENT}/${SHARD_TOTAL}] ${scenario} + ${fault} (comboIndex=${THIS_INDEX}/${TOTAL_COMBOS}) ==="

        EXPERIMENT_ID="${EXPERIMENT_ID}" \
        RESULTS_DIR="${COMBO_DIR}" \
            ./scripts/run_experiment.sh "${scenario}" "${fault}" "${REPEATS}"

        # Optional: sync completed combo to GCS to avoid local disk growth.
        if [ -n "${GCS_RESULTS_URI:-}" ] || [ -n "${GCS_BUCKET:-}" ]; then
            if bash ./scripts/gcs/sync.sh "${COMBO_DIR}"; then
                if [ "${GCS_PRUNE_LOCAL_AFTER_UPLOAD:-0}" = "1" ]; then
                    bash ./scripts/gcs/prune_local.sh "${COMBO_DIR}" || true
                fi
                if [ "${GCS_DELETE_LOCAL_AFTER_UPLOAD:-0}" = "1" ]; then
                    rm -rf "${COMBO_DIR}" || true
                fi
            else
                echo "WARNING: GCS sync failed for ${COMBO_DIR} (not pruning local)" >&2
            fi
        fi

        echo "[${CURRENT}/${SHARD_TOTAL}] Done: ${scenario} + ${fault}"
    done
done

echo ""
echo "=============================================="
echo "  Grid Complete"
echo "  Results in: ${GRID_DIR}"
echo "=============================================="
echo ""
echo "Run analysis:"
echo "  python evaluation/analyse.py ${GRID_DIR} ${GRID_DIR}/reports"
echo "  python evaluation/visualise.py ${GRID_DIR} ${GRID_DIR}/plots"

if [ "${GCS_DELETE_LOCAL_AFTER_UPLOAD:-0}" = "1" ]; then
    echo ""
    echo "NOTE: Local combo dirs were deleted after upload." 
    echo "Download a minimal copy for analysis, e.g.:"
    echo "  GCS_RESULTS_URI=... bash ./scripts/gcs/download_min.sh ${GRID_DIR}"
fi

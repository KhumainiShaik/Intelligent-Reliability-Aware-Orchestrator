#!/usr/bin/env bash
set -euo pipefail

# 15_run_autoscaling_stress_validation.sh — full action-set validation for the
# final dissertation claim: reliability-first adaptive rollout orchestration
# under autoscaling stress.
#
# This completes the declared action set:
#   - traffic profiles that exercise HPA/autoscaling pressure: ramp, spike
#   - reliability-oriented adaptive policy with rollout hints
#   - fixed baselines: Rolling, Canary, Pre-Scale, Delay
#   - deterministic rule-based selector baseline
#   - no-fault plus Litmus pod-kill and network-latency
#
# Defaults:
#   6 modes x 2 scenarios x 3 faults x 3 repeats = 108 trials.
#
# The default uses three shards rather than six. Each shard runs two
# scenario/fault combinations sequentially, which keeps the validation focused
# while reducing local kubectl/k6/API pressure during long publication runs.
#
# Required env:
#   WORK_IMAGE_REPO
#
# Useful env:
#   COMPARISON_TIMESTAMP=20260505_autoscaling_stress
#   SHARD_COUNT=3
#   REPEATS=3
#   GCS_RESULTS_URI=gs://...

COMPARISON_TIMESTAMP="${COMPARISON_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)_autoscaling_stress}"
SHARD_COUNT="${SHARD_COUNT:-3}"
REPEATS="${REPEATS:-3}"
WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"

if [ -z "${WORK_IMAGE_REPO}" ]; then
  echo "ERROR: Set WORK_IMAGE_REPO, for example:" >&2
  echo "  WORK_IMAGE_REPO=europe-west2-docker.pkg.dev/<project>/oroll/orchestrated-rollout-workload" >&2
  exit 2
fi

export COMPARISON_TIMESTAMP
export SHARD_COUNT
export REPEATS
export WORK_IMAGE_REPO

# Claim-specific matrix.
export MODES_CSV="${MODES_CSV:-rl,rule-based,rolling,canary,pre-scale,delay}"
export SCENARIOS_CSV="${SCENARIOS_CSV:-ramp,spike}"
export FAULTS_CSV="${FAULTS_CSV:-none,pod-kill,network-latency}"
export ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT:-1}"
export ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE:-reliability}"

# Keep local artifacts analysis-friendly.
export REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP:-1}"
export REQUIRE_TRIAL_METRICS="${REQUIRE_TRIAL_METRICS:-1}"
export K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON:-0}"
export K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON:-1}"

# Preserve the same stress profile as the successful full/focused GKE runs.
export K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS:-60}"
export CHAOS_CLEANUP_WAIT_SECONDS="${CHAOS_CLEANUP_WAIT_SECONDS:-120}"
export POST_TRIAL_COOLDOWN_SECONDS="${POST_TRIAL_COOLDOWN_SECONDS:-60}"
export SLO_P95_MS="${SLO_P95_MS:-100}"
export KUBECTL_RETRY_ATTEMPTS="${KUBECTL_RETRY_ATTEMPTS:-20}"
export KUBECTL_RETRY_DELAY_SECONDS="${KUBECTL_RETRY_DELAY_SECONDS:-5}"
export DEPLOYMENT_RESET_TIMEOUT_SECONDS="${DEPLOYMENT_RESET_TIMEOUT_SECONDS:-420}"

echo "=============================================="
echo "  Autoscaling-Stress Publication Validation"
echo "  Timestamp:  ${COMPARISON_TIMESTAMP}"
echo "  Claim:      reliability-first adaptive rollout orchestration"
echo "  Modes:      ${MODES_CSV}"
echo "  Scenarios:  ${SCENARIOS_CSV}"
echo "  Faults:     ${FAULTS_CSV}"
echo "  Repeats:    ${REPEATS}"
echo "  Shards:     ${SHARD_COUNT}"
echo "=============================================="

./scripts/gke/14_run_targeted_v11_validation.sh

RESULTS_DIR="results/comparison_${COMPARISON_TIMESTAMP}"
EXPERIMENTS_OUT="${RESULTS_DIR}/experiments"
mkdir -p "${EXPERIMENTS_OUT}"

mode_timestamp() {
  local mode="$1"
  if [ "${mode}" = "rl" ] || [ "${mode}" = "rl-v11" ] || [ "${mode}" = "rl-v12" ]; then
    echo "${COMPARISON_TIMESTAMP}_${mode}"
  else
    echo "${COMPARISON_TIMESTAMP}_baseline-${mode}"
  fi
}

IFS=',' read -r -a _modes <<< "${MODES_CSV}"
for mode in "${_modes[@]}"; do
  mode="${mode//[[:space:]]/}"
  [ -n "${mode}" ] || continue
  mode_ts="$(mode_timestamp "${mode}")"
  found=0
  for shard_dir in experiments/grid_"${mode_ts}"_shard*-of-*; do
    [ -d "${shard_dir}" ] || continue
    found=1
    link="${EXPERIMENTS_OUT}/$(basename "${shard_dir}")"
    if [ ! -e "${link}" ]; then
      ln -s "../../../${shard_dir}" "${link}"
    fi
  done
  if [ "${found}" -ne 1 ]; then
    echo "ERROR: no shard outputs found for ${mode_ts}" >&2
    exit 1
  fi
done

RL_MODE_LABEL="${RL_MODE_LABEL:-RL/adaptive headroom}" \
  python3 -m evaluation.compare_modes "${RESULTS_DIR}"

RL_MODE_LABEL="${RL_MODE_LABEL:-RL/adaptive headroom}" \
  python3 evaluation/summarise_comparison_metrics.py "${RESULTS_DIR}"

python3 evaluation/final_statistics.py "${RESULTS_DIR}"

echo "=============================================="
echo "  Autoscaling-Stress Validation Complete"
echo "  Results: ${RESULTS_DIR}/reports"
echo "=============================================="

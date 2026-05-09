#!/usr/bin/env bash
set -euo pipefail

# 14_run_targeted_v11_validation.sh — targeted rerun for the v11 no-forecast
# policy plus optional rollout traffic hints.
#
# Supports the final six-mode action-set validation:
#   1. RL/adaptive policy with rolloutHints.trafficProfile
#   2. rule-based deterministic selector
#   3. baseline-rolling
#   4. baseline-canary
#   5. baseline-pre-scale
#   6. baseline-delay
#
# Coverage defaults to all scenarios and all faults, 3 repeats:
#   6 modes x 3 scenarios x 3 faults x 3 repeats = 162 trials.
#
# Required env:
#   WORK_IMAGE_REPO
#
# Useful env:
#   SHARD_COUNT=9
#   REPEATS=3
#   SCENARIOS_CSV=steady,ramp,spike
#   FAULTS_CSV=none,pod-kill,network-latency
#   MODES_CSV=rl,rule-based,rolling,canary,pre-scale,delay
#   ENABLE_ROLLOUT_TRAFFIC_HINT=1
#   ROLLOUT_OBJECTIVE=reliability
#   COMPARISON_TIMESTAMP=20260502_v11_hint

COMPARISON_TIMESTAMP="${COMPARISON_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)_v11_targeted}"
SHARD_COUNT="${SHARD_COUNT:-9}"
REPEATS="${REPEATS:-3}"
SCENARIOS_CSV="${SCENARIOS_CSV:-steady,ramp,spike}"
FAULTS_CSV="${FAULTS_CSV:-none,pod-kill,network-latency}"
MODES_CSV="${MODES_CSV:-rl,rule-based,rolling,canary,pre-scale,delay}"
ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT:-1}"
ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE:-reliability}"
ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT:-}"
REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP:-1}"
K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON:-0}"
K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON:-1}"

MODES=()
IFS=',' read -r -a _MODE_ITEMS <<< "${MODES_CSV}"
for _mode in "${_MODE_ITEMS[@]}"; do
  _mode="${_mode//[[:space:]]/}"
  [ -n "${_mode}" ] && MODES+=("${_mode}")
done
if [ "${#MODES[@]}" -eq 0 ]; then
  echo "ERROR: MODES_CSV produced no modes." >&2
  exit 1
fi

count_csv() {
  local value="$1"
  local count=0
  local item
  IFS=',' read -r -a _items <<< "${value}"
  for item in "${_items[@]}"; do
    item="${item//[[:space:]]/}"
    [ -n "${item}" ] && count=$((count + 1))
  done
  echo "${count}"
}

SCENARIO_COUNT="$(count_csv "${SCENARIOS_CSV}")"
FAULT_COUNT="$(count_csv "${FAULTS_CSV}")"
TOTAL_TRIALS=$(( ${#MODES[@]} * SCENARIO_COUNT * FAULT_COUNT * REPEATS ))

echo "=============================================="
echo "  Targeted v11 Validation Run"
echo "  Timestamp:     ${COMPARISON_TIMESTAMP}"
echo "  Shards:        ${SHARD_COUNT}"
echo "  Repeats:       ${REPEATS}"
echo "  Scenarios:     ${SCENARIOS_CSV}"
echo "  Faults:        ${FAULTS_CSV}"
echo "  Modes:         ${MODES[*]}"
echo "  Traffic hints: ${ENABLE_ROLLOUT_TRAFFIC_HINT}"
echo "  Objective:     ${ROLLOUT_OBJECTIVE}"
echo "  Total trials:  ${TOTAL_TRIALS}"
echo "=============================================="

for mode in "${MODES[@]}"; do
  echo ""
  echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
  echo "  Starting targeted mode: ${mode}"
  echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"

  if [ "${mode}" = "rl" ]; then
    export ACTION_SET=""
    grid_ts="${COMPARISON_TIMESTAMP}_rl"
    export ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT}"
    export ROLLOUT_MAX_EXTRA_REPLICAS=""
    export ROLLOUT_MAX_DELAY_SECONDS=""
  elif [ "${mode}" = "rl-v11" ]; then
    export ACTION_SET=""
    grid_ts="${COMPARISON_TIMESTAMP}_rl-v11"
    export ROLLOUT_POLICY_VARIANT=""
    export ROLLOUT_MAX_EXTRA_REPLICAS=""
    export ROLLOUT_MAX_DELAY_SECONDS=""
  elif [ "${mode}" = "rl-v12" ]; then
    export ACTION_SET=""
    grid_ts="${COMPARISON_TIMESTAMP}_rl-v12"
    export ROLLOUT_POLICY_VARIANT="v12-contextual"
    export ROLLOUT_MAX_EXTRA_REPLICAS="${V12_MAX_EXTRA_REPLICAS:-7}"
    export ROLLOUT_MAX_DELAY_SECONDS="${V12_MAX_DELAY_SECONDS:-30}"
  else
    export ACTION_SET="${mode}"
    grid_ts="${COMPARISON_TIMESTAMP}_baseline-${mode}"
    export ROLLOUT_POLICY_VARIANT=""
    export ROLLOUT_MAX_EXTRA_REPLICAS=""
    export ROLLOUT_MAX_DELAY_SECONDS=""
  fi

  export GRID_TIMESTAMP="${grid_ts}"
  export SHARD_COUNT="${SHARD_COUNT}"
  export REPEATS="${REPEATS}"
  export SCENARIOS_CSV="${SCENARIOS_CSV}"
  export FAULTS_CSV="${FAULTS_CSV}"
  export ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT}"
  export ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE}"
  export ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT}"
  export ROLLOUT_MAX_EXTRA_REPLICAS="${ROLLOUT_MAX_EXTRA_REPLICAS:-}"
  export ROLLOUT_MAX_DELAY_SECONDS="${ROLLOUT_MAX_DELAY_SECONDS:-}"
  export REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP}"
  export K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON}"
  export K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON}"

  ./scripts/gke/10_run_grid_shards.sh

  echo ""
  echo "<<< Targeted mode ${mode} complete >>>"
  echo ""
done

echo "=============================================="
echo "  Targeted v11 Validation Complete"
echo "  Timestamp: ${COMPARISON_TIMESTAMP}"
echo "  Analyse with:"
for mode in "${MODES[@]}"; do
  if [ "${mode}" = "rl" ]; then
    echo "    python3 scripts/combine_shard_grids.py ${COMPARISON_TIMESTAMP}_rl"
  else
    echo "    python3 scripts/combine_shard_grids.py ${COMPARISON_TIMESTAMP}_baseline-${mode}"
  fi
done
echo "=============================================="

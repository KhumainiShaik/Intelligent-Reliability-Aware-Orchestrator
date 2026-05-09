#!/usr/bin/env bash
set -euo pipefail

# Run only rows listed in a balanced-matrix missing_trials.csv.

MISSING_TRIALS_CSV="${MISSING_TRIALS_CSV:-}"
COMPARISON_TIMESTAMP="${COMPARISON_TIMESTAMP:-}"
SHARD_COUNT="${SHARD_COUNT:-3}"
PARALLEL_SHARD_WORKERS="${PARALLEL_SHARD_WORKERS:-${SHARD_COUNT}}"
NAMESPACE_PREFIX="${NAMESPACE_PREFIX:-orchestrated-rollout-s}"
INGRESS_HOST_PREFIX="${INGRESS_HOST_PREFIX:-workload-s}"
BASE_INGRESS_PORT="${BASE_INGRESS_PORT:-8880}"
WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"
ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT:-1}"
ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE:-reliability}"
VALIDATION_PROFILE="${VALIDATION_PROFILE:-fast}"
RELEASE_TAG="${RELEASE_TAG:-v2.0.0}"
REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP:-1}"
REQUIRE_TRIAL_METRICS="${REQUIRE_TRIAL_METRICS:-1}"
K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON:-0}"
K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON:-1}"

if [ -z "${MISSING_TRIALS_CSV}" ] || [ ! -f "${MISSING_TRIALS_CSV}" ]; then
  echo "ERROR: set MISSING_TRIALS_CSV to an existing missing_trials.csv" >&2
  exit 2
fi
if [ -z "${COMPARISON_TIMESTAMP}" ]; then
  echo "ERROR: set COMPARISON_TIMESTAMP, e.g. 20260508_balanced_actionset_72" >&2
  exit 2
fi
if [ -z "${WORK_IMAGE_REPO}" ]; then
  echo "ERROR: set WORK_IMAGE_REPO" >&2
  exit 2
fi

RESULTS_DIR="results/comparison_${COMPARISON_TIMESTAMP}"
EXPERIMENTS_DIR="${RESULTS_DIR}/experiments"
mkdir -p "${EXPERIMENTS_DIR}" "${RESULTS_DIR}/reports"

INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP:-}"
if [ -z "${INGRESS_EXTERNAL_IP}" ]; then
  INGRESS_EXTERNAL_IP="$(kubectl get svc ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
fi

echo "=============================================="
echo "  Missing Trial Top-up"
echo "  Timestamp: ${COMPARISON_TIMESTAMP}"
echo "  Missing CSV: ${MISSING_TRIALS_CSV}"
echo "  Shards: ${SHARD_COUNT}"
echo "  Parallel workers: ${PARALLEL_SHARD_WORKERS}"
echo "  Profile: ${VALIDATION_PROFILE}"
echo "=============================================="

status_csv="${RESULTS_DIR}/missing_trials_run_status.csv"
echo "mode,scenario,fault,repeat,shard,status,details" > "${status_csv}"

mode_token() {
  case "$1" in
    rl|rl-v11|rl-v12) echo "$1" ;;
    *) echo "baseline-$1" ;;
  esac
}

action_set_for_mode() {
  case "$1" in
    rl|rl-v11|rl-v12) echo "" ;;
    *) echo "$1" ;;
  esac
}

policy_variant_for_mode() {
  case "$1" in
    rl-v12) echo "v12-contextual" ;;
    *) echo "" ;;
  esac
}

run_missing_row() {
  local mode="$1"
  local scenario="$2"
  local fault="$3"
  local repeat="$4"
  local shard="$5"
  local status_file="$6"

  [ -n "${mode}" ] || return 0
  local token
  local action_set
  local policy_variant
  local shard_index
  local ns
  local host
  local port
  local combo_dir
  local log_dir
  local log_file
  local metrics
  local rc

  token="$(mode_token "${mode}")"
  action_set="$(action_set_for_mode "${mode}")"
  policy_variant="$(policy_variant_for_mode "${mode}")"
  shard_index=$((shard % SHARD_COUNT))
  ns="${NAMESPACE_PREFIX}${shard_index}"
  host="${INGRESS_HOST_PREFIX}${shard_index}.local"
  port=$((BASE_INGRESS_PORT + shard_index))
  combo_dir="${EXPERIMENTS_DIR}/grid_${COMPARISON_TIMESTAMP}_${token}_shard${shard_index}-of-${SHARD_COUNT}/${scenario}_${fault}"
  log_dir="${RESULTS_DIR}/logs"
  mkdir -p "${combo_dir}" "${log_dir}"
  log_file="${log_dir}/${mode}_${scenario}_${fault}_r${repeat}.log"

  metrics="${combo_dir}/reports/trial_${repeat}_metrics.json"
  if [ -f "${metrics}" ]; then
    echo "${mode},${scenario},${fault},${repeat},${shard_index},skipped,metrics already present" >> "${status_file}"
    return 0
  fi

  echo ""
  echo ">>> ${mode} ${scenario} ${fault} repeat=${repeat} shard=${shard_index}"

  set +e
  (
    export NAMESPACE="${ns}"
    export INGRESS_HOST_HEADER="${host}"
    export INGRESS_PORT="${port}"
    export INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP}"
    export ACTION_SET="${action_set}"
    export ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT}"
    export ROLLOUT_TRAFFIC_PROFILE="${scenario}"
    export ROLLOUT_FAULT_CONTEXT="${fault}"
    export ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE}"
    export ROLLOUT_POLICY_VARIANT="${policy_variant}"
    export VALIDATION_PROFILE="${VALIDATION_PROFILE}"
    export WORK_IMAGE_REPO="${WORK_IMAGE_REPO}"
    export RELEASE_IMAGE="${WORK_IMAGE_REPO}"
    export RELEASE_TAG="${RELEASE_TAG}"
    export REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP}"
    export REQUIRE_TRIAL_METRICS="${REQUIRE_TRIAL_METRICS}"
    export K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON}"
    export K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON}"
    export REPEAT_START="${repeat}"
    export REPEAT_END="${repeat}"
    export EXPERIMENT_ID="balanced_${mode}_${scenario}_${fault}_r${repeat}_${COMPARISON_TIMESTAMP}"
    export RESULTS_DIR="${combo_dir}"
    ./scripts/run_experiment.sh "${scenario}" "${fault}" "${repeat}"
  ) > "${log_file}" 2>&1
  rc=$?
  set -e

  kubectl delete oroll --all -n "${ns}" --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl delete rollout --all -n "${ns}" --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl delete chaosengine --all -n "${ns}" --ignore-not-found=true >/dev/null 2>&1 || true

  if [ "${rc}" -eq 0 ] && [ -f "${metrics}" ]; then
    echo "${mode},${scenario},${fault},${repeat},${shard_index},completed,${metrics}" >> "${status_file}"
  else
    echo "${mode},${scenario},${fault},${repeat},${shard_index},failed,see ${log_file}" >> "${status_file}"
  fi
}

run_worker() {
  local worker="$1"
  local worker_status="${RESULTS_DIR}/missing_trials_run_status.worker${worker}.csv"
  : > "${worker_status}"
  tail -n +2 "${MISSING_TRIALS_CSV}" | while IFS=',' read -r mode scenario fault repeat shard _status; do
    [ -n "${mode}" ] || continue
    if [ $((shard % PARALLEL_SHARD_WORKERS)) -ne "${worker}" ]; then
      continue
    fi
    run_missing_row "${mode}" "${scenario}" "${fault}" "${repeat}" "${shard}" "${worker_status}"
  done
}

pids=()
for worker in $(seq 0 $((PARALLEL_SHARD_WORKERS - 1))); do
  run_worker "${worker}" &
  pids+=("$!")
done

for pid in "${pids[@]}"; do
  wait "${pid}"
done

for worker in $(seq 0 $((PARALLEL_SHARD_WORKERS - 1))); do
  worker_status="${RESULTS_DIR}/missing_trials_run_status.worker${worker}.csv"
  if [ -f "${worker_status}" ]; then
    cat "${worker_status}" >> "${status_csv}"
    rm -f "${worker_status}"
  fi
done

echo "Top-up status: ${status_csv}"

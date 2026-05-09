#!/usr/bin/env bash
set -euo pipefail

# 10_run_grid_shards.sh — launch shard runs in parallel from your local machine.
#
# Assumes shard namespaces are already deployed (see 09_deploy_shards.sh).
# Each shard uses:
#   - a unique namespace
#   - a unique ingress host header
#   - a unique local port for port-forwarding
#
# Required env:
#   WORK_IMAGE_REPO  (used as RELEASE_IMAGE)
# Optional env:
#   SHARD_COUNT (default 9)
#   NAMESPACE_PREFIX (default orchestrated-rollout-s)
#   INGRESS_HOST_PREFIX (default workload-s)
#   BASE_INGRESS_PORT (default 8880)
#   REPEATS (default 5)
#   RELEASE_TAG (default v2.0.0)
#   GRID_TIMESTAMP (default now)
#
# Optional GCS offload (to reduce local disk use):
#   GCS_RESULTS_URI (e.g. gs://my-bucket/oroll-results)
#     OR GCS_BUCKET + optional GCS_PREFIX
#   GCS_EXCLUDE_REGEX (optional gsutil rsync -x regex)
#   GCS_PRUNE_LOCAL_AFTER_UPLOAD=1 (delete large local artifacts after successful upload)
#   K6_SAVE_RAW_JSON=0 (avoid huge k6 raw JSON; keeps stdout + summary)

SHARD_COUNT="${SHARD_COUNT:-9}"
SHARD_START="${SHARD_START:-0}"  # first shard index (for parallel mode runs on disjoint shard subsets)
NAMESPACE_PREFIX="${NAMESPACE_PREFIX:-orchestrated-rollout-s}"
INGRESS_HOST_PREFIX="${INGRESS_HOST_PREFIX:-workload-s}"
BASE_INGRESS_PORT="${BASE_INGRESS_PORT:-8880}"
REPEATS="${REPEATS:-5}"
REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP:-1}"
ACTION_SET="${ACTION_SET:-}"
ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT:-1}"
ROLLOUT_TRAFFIC_PROFILE="${ROLLOUT_TRAFFIC_PROFILE:-}"
ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE:-reliability}"
ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT:-}"
ROLLOUT_MAX_EXTRA_REPLICAS="${ROLLOUT_MAX_EXTRA_REPLICAS:-}"
ROLLOUT_MAX_DELAY_SECONDS="${ROLLOUT_MAX_DELAY_SECONDS:-}"
CHAOS_CLEANUP_WAIT_SECONDS="${CHAOS_CLEANUP_WAIT_SECONDS:-60}"
POST_TRIAL_COOLDOWN_SECONDS="${POST_TRIAL_COOLDOWN_SECONDS:-0}"

GCS_RESULTS_URI="${GCS_RESULTS_URI:-}"
GCS_BUCKET="${GCS_BUCKET:-}"
GCS_PREFIX="${GCS_PREFIX:-}"
GCS_EXCLUDE_REGEX="${GCS_EXCLUDE_REGEX:-}"
GCS_PRUNE_LOCAL_AFTER_UPLOAD="${GCS_PRUNE_LOCAL_AFTER_UPLOAD:-0}"
GCS_DELETE_LOCAL_AFTER_UPLOAD="${GCS_DELETE_LOCAL_AFTER_UPLOAD:-0}"
K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON:-1}"
K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON:-1}"

# SLO threshold for k6 (ms). Raise for remote testing (Mac → GKE adds RTT).
SLO_P95_MS="${SLO_P95_MS:-100}"
# k6 warm-up seconds before OrchestratedRollout creation.
K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS:-60}"

WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"
RELEASE_TAG="${RELEASE_TAG:-v2.0.0}"

GRID_TIMESTAMP="${GRID_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"

if [ -z "${WORK_IMAGE_REPO}" ]; then
  echo "ERROR: Set WORK_IMAGE_REPO (used as RELEASE_IMAGE)" >&2
  exit 2
fi

mkdir -p experiments

# Auto-discover NGINX Ingress LoadBalancer external IP (GKE).
# This eliminates port-forwarding entirely — k6 hits the LB directly.
INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP:-}"
if [ -z "${INGRESS_EXTERNAL_IP}" ]; then
  INGRESS_EXTERNAL_IP=$(kubectl get svc ingress-nginx-controller -n ingress-nginx \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
fi
if [ -n "${INGRESS_EXTERNAL_IP}" ]; then
  echo "Using LoadBalancer IP: ${INGRESS_EXTERNAL_IP} (no port-forward needed)"
else
  echo "No LoadBalancer IP found; will fall back to port-forward per shard"
fi

echo "Launching ${SHARD_COUNT} shards from index ${SHARD_START} (GRID_TIMESTAMP=${GRID_TIMESTAMP})"
echo "Rollout traffic hints: ENABLE_ROLLOUT_TRAFFIC_HINT=${ENABLE_ROLLOUT_TRAFFIC_HINT}"
echo "Rollout objective: ${ROLLOUT_OBJECTIVE}"
echo "Rollout policy variant: ${ROLLOUT_POLICY_VARIANT:-v11/default}"
echo "Trial isolation: CHAOS_CLEANUP_WAIT_SECONDS=${CHAOS_CLEANUP_WAIT_SECONDS}, POST_TRIAL_COOLDOWN_SECONDS=${POST_TRIAL_COOLDOWN_SECONDS}"

pids=()
for i in $(seq ${SHARD_START} $((SHARD_START + SHARD_COUNT - 1))); do
  ns="${NAMESPACE_PREFIX}${i}"
  host="${INGRESS_HOST_PREFIX}${i}.local"
  port=$((BASE_INGRESS_PORT + i))

  log="experiments/grid_${GRID_TIMESTAMP}_shard${i}.log"

  echo "- shard ${i}: ns=${ns} host=${host} log=${log}"

  (
    export SHARD_COUNT="${SHARD_COUNT}"
    export SHARD_INDEX="$((i - SHARD_START))"  # relative index for scenario modulo distribution
    export GRID_TIMESTAMP="${GRID_TIMESTAMP}"
    export REPEATS="${REPEATS}"

    # Optional: force a fixed action/strategy for baseline comparisons.
    # Examples: ACTION_SET="" (rl) | "rule-based" | "rolling" | "canary" | "pre-scale" | "delay"
    export ACTION_SET="${ACTION_SET}"
    export ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT}"
    export ROLLOUT_TRAFFIC_PROFILE="${ROLLOUT_TRAFFIC_PROFILE}"
    export ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE}"
    export ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT}"
    export ROLLOUT_MAX_EXTRA_REPLICAS="${ROLLOUT_MAX_EXTRA_REPLICAS}"
    export ROLLOUT_MAX_DELAY_SECONDS="${ROLLOUT_MAX_DELAY_SECONDS}"
    export CHAOS_CLEANUP_WAIT_SECONDS="${CHAOS_CLEANUP_WAIT_SECONDS}"
    export POST_TRIAL_COOLDOWN_SECONDS="${POST_TRIAL_COOLDOWN_SECONDS}"

    # Optional: offload artifacts to GCS to reduce local disk usage.
    export GCS_RESULTS_URI="${GCS_RESULTS_URI}"
    export GCS_BUCKET="${GCS_BUCKET}"
    export GCS_PREFIX="${GCS_PREFIX}"
    export GCS_EXCLUDE_REGEX="${GCS_EXCLUDE_REGEX}"
    export GCS_PRUNE_LOCAL_AFTER_UPLOAD="${GCS_PRUNE_LOCAL_AFTER_UPLOAD}"
    export GCS_DELETE_LOCAL_AFTER_UPLOAD="${GCS_DELETE_LOCAL_AFTER_UPLOAD}"
    export K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON}"
    export K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON}"
    export SLO_P95_MS="${SLO_P95_MS}"
    export K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS}"

    export NAMESPACE="${ns}"
    export INGRESS_HOST_HEADER="${host}"

    # Use LoadBalancer IP when available (GKE); fall back to port-forward (kind).
    if [ -n "${INGRESS_EXTERNAL_IP}" ]; then
      export INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP}"
    else
      export INGRESS_PORT="${port}"
      export AUTO_PORT_FORWARD=1
    fi

    export REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP}"
    export RELEASE_IMAGE="${WORK_IMAGE_REPO}"
    export RELEASE_TAG="${RELEASE_TAG}"

    ./scripts/full_experiment_grid.sh
  ) >"${log}" 2>&1 &

  pids+=("$!")

  # Stagger shard starts to prevent initial burst overloading the cluster.
  # 9 shards × 100 RPS = 900 RPS; give HPA time to react between launches.
  if [ "${i}" -lt $((SHARD_START + SHARD_COUNT - 1)) ]; then
    echo "  Waiting 15s before next shard launch (stagger)..."
    sleep 15
  fi

done

echo ""
echo "All shards launched."
echo "PIDs: ${pids[*]}"
echo "Tail logs e.g.: tail -f experiments/grid_${GRID_TIMESTAMP}_shard0.log"

failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    echo "ERROR: shard process ${pid} failed" >&2
    failed=1
  fi
done

if [ "${failed}" -ne 0 ]; then
  echo "ERROR: one or more shards failed for GRID_TIMESTAMP=${GRID_TIMESTAMP}" >&2
  exit 1
fi

# Optional: upload shard logs after completion.
if [ -n "${GCS_RESULTS_URI}" ] || [ -n "${GCS_BUCKET}" ]; then
  for i in $(seq ${SHARD_START} $((SHARD_START + SHARD_COUNT - 1))); do
    log="experiments/grid_${GRID_TIMESTAMP}_shard${i}.log"
    if [ -f "${log}" ]; then
      bash ./scripts/gcs/sync.sh "${log}" || echo "WARNING: failed to upload log ${log}" >&2
    fi
  done
fi

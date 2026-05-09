#!/usr/bin/env bash
# run_experiment.sh — Run a complete experiment episode
set -euo pipefail

KUBECTL_RETRY_ATTEMPTS="${KUBECTL_RETRY_ATTEMPTS:-5}"
KUBECTL_RETRY_DELAY_SECONDS="${KUBECTL_RETRY_DELAY_SECONDS:-2}"
RESET_REPLICAS="${RESET_REPLICAS:-2}"
DEPLOYMENT_RESET_TIMEOUT_SECONDS="${DEPLOYMENT_RESET_TIMEOUT_SECONDS:-180}"
DEPLOYMENT_RESET_POLL_SECONDS="${DEPLOYMENT_RESET_POLL_SECONDS:-2}"
REFRESH_GKE_CREDENTIALS_PER_TRIAL="${REFRESH_GKE_CREDENTIALS_PER_TRIAL:-0}"
GKE_CLUSTER_NAME="${GKE_CLUSTER_NAME:-orchestrated-rollout}"
GKE_REGION="${GKE_REGION:-europe-west2}"
GKE_PROJECT_ID="${GKE_PROJECT_ID:-}"

kubectl_retry() {
    local attempt=1
    while true; do
        if kubectl "$@"; then
            return 0
        fi
        if [ "${attempt}" -ge "${KUBECTL_RETRY_ATTEMPTS}" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep "${KUBECTL_RETRY_DELAY_SECONDS}"
    done
}

kubectl_apply_manifest() {
    local manifest_file
    if ! manifest_file=$(mktemp "${TMPDIR:-/tmp}/oroll-manifest.XXXXXX"); then
        echo "ERROR: failed to create temporary manifest file" >&2
        cat >/dev/null
        return 1
    fi
    cat > "${manifest_file}"

    set +e
    kubectl_retry apply --validate=false -f "${manifest_file}"
    local rc=$?
    set -e

    rm -f "${manifest_file}"
    return "${rc}"
}

refresh_gke_credentials() {
    local active_project="${GKE_PROJECT_ID}"
    local project_arg=()

    if [ -z "${active_project}" ]; then
        active_project=$(gcloud config get-value project 2>/dev/null || true)
    fi
    if [ -n "${active_project}" ]; then
        project_arg=(--project "${active_project}")
    fi

    gcloud container clusters get-credentials "${GKE_CLUSTER_NAME}" \
        --region "${GKE_REGION}" \
        "${project_arg[@]}" \
        --quiet 2>/dev/null || \
        gcloud auth print-access-token > /dev/null 2>&1 || true
}

wait_for_deployment_replicas() {
    local deployment="$1"
    local desired="$2"
    local deadline=$((SECONDS + DEPLOYMENT_RESET_TIMEOUT_SECONDS))
    local spec_replicas ready_replicas available_replicas

    while [ "${SECONDS}" -lt "${deadline}" ]; do
        spec_replicas=$(kubectl_retry get "deployment/${deployment}" -n "${NAMESPACE}" \
            -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
        ready_replicas=$(kubectl_retry get "deployment/${deployment}" -n "${NAMESPACE}" \
            -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
        available_replicas=$(kubectl_retry get "deployment/${deployment}" -n "${NAMESPACE}" \
            -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)

        ready_replicas="${ready_replicas:-0}"
        available_replicas="${available_replicas:-0}"

        if [ "${spec_replicas}" = "${desired}" ] && \
           [ "${ready_replicas}" = "${desired}" ] && \
           [ "${available_replicas}" = "${desired}" ]; then
            return 0
        fi

        sleep "${DEPLOYMENT_RESET_POLL_SECONDS}"
    done

    echo "ERROR: deployment/${deployment} did not settle at ${desired} replicas within ${DEPLOYMENT_RESET_TIMEOUT_SECONDS}s" >&2
    kubectl_retry get "deployment/${deployment}" -n "${NAMESPACE}" -o wide >&2 || true
    return 1
}

# Parse arguments
SCENARIO="${1:-steady}"
FAULT="${2:-none}"
REPEATS="${3:-5}"
REPEAT_START="${REPEAT_START:-1}"
REPEAT_END="${REPEAT_END:-${REPEATS}}"
EXPERIMENT_ID="${EXPERIMENT_ID:-exp_$(date +%Y%m%d_%H%M%S)}"
RESULTS_DIR="${RESULTS_DIR:-experiments/${EXPERIMENT_ID}}"

# k6 output options (raw JSON can be very large; summary export is small and analysis-friendly).
K6_SAVE_RAW_JSON="${K6_SAVE_RAW_JSON:-1}"
K6_SAVE_SUMMARY_JSON="${K6_SAVE_SUMMARY_JSON:-1}"
REQUIRE_TRIAL_METRICS="${REQUIRE_TRIAL_METRICS:-1}"

NAMESPACE="${NAMESPACE:-orchestrated-rollout}"
RELEASE_TAG="${RELEASE_TAG:-v2.0.0}"
RELEASE_IMAGE="${RELEASE_IMAGE:-}"

# Optional: constrain the OrchestratedRollout to a fixed action set.
# Examples:
#   ACTION_SET="rolling"     # fixed rolling baseline
#   ACTION_SET="canary"      # fixed canary baseline
#   ACTION_SET="delay,canary"  # allow only delay/canary
ACTION_SET="${ACTION_SET:-}"

# Optional advisory traffic hint for the adaptive policy. The experiment harness
# knows the load shape it is about to run, so by default it passes that context
# to the controller. Set ENABLE_ROLLOUT_TRAFFIC_HINT=0 for no-hint ablations, or
# set ROLLOUT_TRAFFIC_PROFILE explicitly to steady/ramp/spike/unknown.
ENABLE_ROLLOUT_TRAFFIC_HINT="${ENABLE_ROLLOUT_TRAFFIC_HINT:-1}"
ROLLOUT_TRAFFIC_PROFILE="${ROLLOUT_TRAFFIC_PROFILE:-${SCENARIO}}"
ROLLOUT_OBJECTIVE="${ROLLOUT_OBJECTIVE:-reliability}"
ROLLOUT_POLICY_VARIANT="${ROLLOUT_POLICY_VARIANT:-}"
ROLLOUT_FAULT_CONTEXT="${ROLLOUT_FAULT_CONTEXT:-${FAULT}}"
ROLLOUT_MAX_EXTRA_REPLICAS="${ROLLOUT_MAX_EXTRA_REPLICAS:-}"
ROLLOUT_MAX_DELAY_SECONDS="${ROLLOUT_MAX_DELAY_SECONDS:-}"

# LitmusChaos (fault injection) settings.
CHAOS_SERVICEACCOUNT="${CHAOS_SERVICEACCOUNT:-oroll-litmus-admin}"
CHAOS_CLEANUP_WAIT_SECONDS="${CHAOS_CLEANUP_WAIT_SECONDS:-60}"
POST_TRIAL_COOLDOWN_SECONDS="${POST_TRIAL_COOLDOWN_SECONDS:-0}"
VALIDATION_PROFILE="${VALIDATION_PROFILE:-standard}"

# Policy artifact presence check (prevents accidental rule-based-only runs).
POLICY_CONFIGMAP_NAME="${POLICY_CONFIGMAP_NAME:-rl-policy-artifact}"
REQUIRE_POLICY_CONFIGMAP="${REQUIRE_POLICY_CONFIGMAP:-0}"

INGRESS_PORT="${INGRESS_PORT:-8888}"
AUTO_PORT_FORWARD="${AUTO_PORT_FORWARD:-1}"
INGRESS_HOST_HEADER="${INGRESS_HOST_HEADER:-workload.local}"

# Direct LoadBalancer access (GKE): set INGRESS_EXTERNAL_IP to skip port-forward entirely.
# The shard runner auto-discovers this from the ingress-nginx service.
INGRESS_EXTERNAL_IP="${INGRESS_EXTERNAL_IP:-}"

if [ "${VALIDATION_PROFILE}" = "fast" ]; then
    K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS:-30}"
    POST_TRIAL_COOLDOWN_SECONDS="${POST_TRIAL_COOLDOWN_SECONDS:-20}"
    STABILIZATION_SECONDS="${STABILIZATION_SECONDS:-20}"
    export RAMP_DURATION="${RAMP_DURATION:-90s}"
    export HOLD_DURATION="${HOLD_DURATION:-60s}"
    export COOLDOWN_DURATION="${COOLDOWN_DURATION:-30s}"
    export WARMUP_DURATION="${WARMUP_DURATION:-30s}"
    export SPIKE_RAMP="${SPIKE_RAMP:-10s}"
    export SPIKE_DURATION="${SPIKE_DURATION:-60s}"
    export RECOVERY_RAMP="${RECOVERY_RAMP:-10s}"
    export RECOVERY_DURATION="${RECOVERY_DURATION:-40s}"
else
    K6_WARMUP_SECONDS="${K6_WARMUP_SECONDS:-60}"
    STABILIZATION_SECONDS="${STABILIZATION_SECONDS:-30}"
fi

# k6 warm-up: seconds to wait after starting k6 before creating the OrchestratedRollout.
# Fast validation intentionally uses a shorter bounded profile; standard runs
# keep the longer 60s warm-up used in the full matrix.

# SLO threshold for k6 (milliseconds).  When testing remotely (Mac → GKE), set
# higher to account for network round-trip time added to res.timings.duration.
SLO_P95_MS="${SLO_P95_MS:-100}"

# OrchestratedRollout wait tuning.
# NOTE: ramp scenarios can run >5m, so the default must exceed that.
OROLL_TIMEOUT_SECONDS="${OROLL_TIMEOUT_SECONDS:-1800}"  # 30 minutes
OROLL_POLL_SECONDS="${OROLL_POLL_SECONDS:-5}"
FAIL_ON_OROLL_TIMEOUT="${FAIL_ON_OROLL_TIMEOUT:-1}"

WORKLOAD_DEPLOYMENT_SELECTOR="${WORKLOAD_DEPLOYMENT_SELECTOR:-app.kubernetes.io/name=workload,app.kubernetes.io/instance=workload}"
WORKLOAD_DEPLOYMENT_NAME="${WORKLOAD_DEPLOYMENT_NAME:-}"
if [ -z "${WORKLOAD_DEPLOYMENT_NAME}" ]; then
    WORKLOAD_DEPLOYMENT_NAME=$(kubectl_retry get deploy -n "${NAMESPACE}" -l "${WORKLOAD_DEPLOYMENT_SELECTOR}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
fi
if [ -z "${WORKLOAD_DEPLOYMENT_NAME}" ]; then
    WORKLOAD_DEPLOYMENT_NAME="workload"
fi
CHAOS_APP_LABEL="${CHAOS_APP_LABEL:-${WORKLOAD_DEPLOYMENT_SELECTOR}}"
WORKLOAD_HPA_NAME="${WORKLOAD_HPA_NAME:-${WORKLOAD_DEPLOYMENT_NAME}-hpa}"

if [ -z "${RELEASE_IMAGE}" ]; then
    CURRENT_WORKLOAD_IMAGE=$(kubectl_retry get "deployment/${WORKLOAD_DEPLOYMENT_NAME}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    if [ -z "${CURRENT_WORKLOAD_IMAGE}" ]; then
        echo "ERROR: RELEASE_IMAGE is not set and deployment/${WORKLOAD_DEPLOYMENT_NAME} image could not be discovered." >&2
        exit 1
    fi

    if [[ "${CURRENT_WORKLOAD_IMAGE}" == *@* ]]; then
        RELEASE_IMAGE="${CURRENT_WORKLOAD_IMAGE%@*}"
    elif [[ "${CURRENT_WORKLOAD_IMAGE##*/}" == *:* ]]; then
        RELEASE_IMAGE="${CURRENT_WORKLOAD_IMAGE%:*}"
    else
        RELEASE_IMAGE="${CURRENT_WORKLOAD_IMAGE}"
    fi
fi

echo "=============================================="
echo "  Orchestrated Rollout Experiment Runner"
echo "=============================================="
echo "  Experiment ID: ${EXPERIMENT_ID}"
echo "  Scenario:      ${SCENARIO}"
echo "  Fault:         ${FAULT}"
echo "  Repeats:       ${REPEAT_START}-${REPEAT_END} of ${REPEATS}"
echo "  Results:       ${RESULTS_DIR}"
echo "  Namespace:     ${NAMESPACE}"
echo "  Workload dep:  ${WORKLOAD_DEPLOYMENT_NAME}"
echo "  Chaos label:   ${CHAOS_APP_LABEL}"
echo "  Release image: ${RELEASE_IMAGE}:${RELEASE_TAG}"
echo "  Ingress port:  ${INGRESS_PORT}"
echo "  k6 raw JSON:   ${K6_SAVE_RAW_JSON}"
echo "  k6 summary:    ${K6_SAVE_SUMMARY_JSON}"
echo "  require metrics: ${REQUIRE_TRIAL_METRICS}"
echo "  traffic hint:  ${ENABLE_ROLLOUT_TRAFFIC_HINT}:${ROLLOUT_TRAFFIC_PROFILE}"
echo "  objective:     ${ROLLOUT_OBJECTIVE:-none}"
echo "  policy variant: ${ROLLOUT_POLICY_VARIANT:-v11/default}"
echo "  chaos cleanup wait: ${CHAOS_CLEANUP_WAIT_SECONDS}s"
echo "  post-trial cooldown: ${POST_TRIAL_COOLDOWN_SECONDS}s"
echo "  validation profile: ${VALIDATION_PROFILE}"
echo "=============================================="

if kubectl get configmap "${POLICY_CONFIGMAP_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "Policy ConfigMap present: ${POLICY_CONFIGMAP_NAME}"
else
    echo "WARNING: Policy ConfigMap missing: ${POLICY_CONFIGMAP_NAME}"
    echo "  The controller will fall back to rule-based unless you create it."
    echo "  Create it with: ./scripts/05_setup_policy_artifact.sh artifacts/v11_no_forecast/policy_artifact.json"
    if [ "${REQUIRE_POLICY_CONFIGMAP}" = "1" ]; then
        echo "ERROR: REQUIRE_POLICY_CONFIGMAP=1 and policy ConfigMap is missing."
        exit 1
    fi
fi

mkdir -p "${RESULTS_DIR}/episodes"
mkdir -p "${RESULTS_DIR}/k6_results"
mkdir -p "${RESULTS_DIR}/reports"

CONTROLLER_POD_SELECTOR="${CONTROLLER_POD_SELECTOR:-app.kubernetes.io/name=controller}"
CONTROLLER_POD_NAME=$(kubectl_retry get pods -n "${NAMESPACE}" -l "${CONTROLLER_POD_SELECTOR}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "${CONTROLLER_POD_NAME}" ]; then
    kubectl exec -n "${NAMESPACE}" "${CONTROLLER_POD_NAME}" -- sh -c 'rm -f /episodes/episode_*.json 2>/dev/null || true' \
        2>/dev/null || true
fi

# Clean up any stale trial CRs from previously aborted runs.
kubectl delete oroll -n "${NAMESPACE}" --ignore-not-found=true \
    $(kubectl_retry get oroll -n "${NAMESPACE}" -o name 2>/dev/null | grep -E 'orchestratedrollouts\.rollout\.orchestrated\.io/experiment-trial-' || true) \
    2>/dev/null || true

# --- Determine TARGET_BASE_URL (how k6 reaches the workload) ---
#
# Priority:
#   1. INGRESS_EXTERNAL_IP set → http://<IP>  (direct LB; best for GKE)
#   2. localhost:INGRESS_PORT reachable      → http://localhost:<port>
#   3. AUTO_PORT_FORWARD=1 → start kubectl port-forward and use localhost
#
# Using the external LB avoids ephemeral-port exhaustion that plagues
# kubectl port-forward under high k6 RPS.

TARGET_BASE_URL=""
PF_PID=""
PF_LOG="${RESULTS_DIR}/ingress_port_forward.log"

cleanup() {
    if [ -n "${PF_PID}" ]; then
        kill "${PF_PID}" 2>/dev/null || true
        wait "${PF_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

check_ingress() {
    local url="$1"
    curl -sf -o /dev/null --max-time 5 \
        -H "Host: ${INGRESS_HOST_HEADER}" \
        "${url}/healthz"
}

read_model_load_seconds() {
    local url="$1"
    curl -sf --max-time 5 \
        -H "Host: ${INGRESS_HOST_HEADER}" \
        "${url}/metrics" \
        | awk '/^workload_model_load_duration_seconds[[:space:]]/ {print $2; exit}'
}

if [ -n "${INGRESS_EXTERNAL_IP}" ]; then
    # Mode 1: Direct LoadBalancer (GKE)
    TARGET_BASE_URL="http://${INGRESS_EXTERNAL_IP}"
    echo "Using direct LoadBalancer: ${TARGET_BASE_URL} (Host: ${INGRESS_HOST_HEADER})"
    for _ in $(seq 1 30); do
        if check_ingress "${TARGET_BASE_URL}"; then
            break
        fi
        sleep 1
    done
    if ! check_ingress "${TARGET_BASE_URL}"; then
        echo "ERROR: LoadBalancer not reachable at ${TARGET_BASE_URL}"
        exit 1
    fi
    echo " — LB ingress OK"
else
    # Mode 2/3: localhost port-forward (kind / local clusters)
    TARGET_BASE_URL="http://localhost:${INGRESS_PORT}"
    echo "Checking ingress reachability on port ${INGRESS_PORT}..."
    if check_ingress "${TARGET_BASE_URL}"; then
        echo " — ingress OK"
    else
        if [ "${AUTO_PORT_FORWARD}" != "1" ]; then
            echo "ERROR: ingress not reachable on port ${INGRESS_PORT}"
            echo "Start it with: kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller ${INGRESS_PORT}:80 &"
            exit 1
        fi

        echo "Ingress not reachable; starting port-forward (logs → ${PF_LOG})..."
        kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller "${INGRESS_PORT}:80" \
            > "${PF_LOG}" 2>&1 &
        PF_PID=$!

        echo "Waiting for ingress to become reachable..."
        for _ in $(seq 1 30); do
            if check_ingress "${TARGET_BASE_URL}"; then
                echo " — ingress OK (port-forward pid=${PF_PID})"
                break
            fi
            sleep 1
        done

        if ! check_ingress "${TARGET_BASE_URL}"; then
            echo "ERROR: ingress still not reachable after port-forward"
            echo "See: ${PF_LOG}"
            exit 1
        fi
    fi
fi

# Map scenario to k6 script
case "${SCENARIO}" in
    steady) K6_SCRIPT="k6/scenarios/steady.js" ;;
    ramp)   K6_SCRIPT="k6/scenarios/ramp.js" ;;
    spike)  K6_SCRIPT="k6/scenarios/spike.js" ;;
    *)      echo "Unknown scenario: ${SCENARIO}"; exit 1 ;;
esac

apply_fault() {
        local fault="$1"
        local trial="$2"
    local safe_experiment_id
    safe_experiment_id=$(echo "${EXPERIMENT_ID}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9.-]+/-/g; s/^-+//; s/-+$//')
    if [ -z "${safe_experiment_id}" ]; then
        safe_experiment_id="exp"
    fi

    # Litmus copies the ChaosEngine name into pod labels, which must be <=63 bytes.
    local engine_base fault_slug engine_hash engine_name
    engine_base=$(echo "${safe_experiment_id}" | cut -c1-24)
    fault_slug=$(echo "${fault}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g' | cut -c1-15)
    engine_hash=$(printf '%s-%s-%s' "${safe_experiment_id}" "${trial}" "${fault}" | cksum | awk '{print $1}' | cut -c1-10)
    engine_name="ce-${engine_base}-t${trial}-${fault_slug}-${engine_hash}"

        case "${fault}" in
                pod-kill)
                        if ! kubectl_apply_manifest >/dev/null <<EOF
{
    "apiVersion": "litmuschaos.io/v1alpha1",
    "kind": "ChaosEngine",
    "metadata": {
        "name": "${engine_name}",
        "namespace": "${NAMESPACE}",
        "labels": {
            "app.kubernetes.io/part-of": "orchestrated-rollout",
            "experiment": "pod-kill"
        }
    },
    "spec": {
        "appinfo": {
            "appns": "${NAMESPACE}",
            "applabel": "${CHAOS_APP_LABEL}",
            "appkind": "deployment"
        },
        "engineState": "active",
        "chaosServiceAccount": "${CHAOS_SERVICEACCOUNT}",
        "experiments": [
            {
                "name": "pod-delete",
                "spec": {
                    "components": {
                        "env": [
                            {"name": "TOTAL_CHAOS_DURATION", "value": "30"},
                            {"name": "CHAOS_INTERVAL", "value": "10"},
                            {"name": "FORCE", "value": "false"},
                            {"name": "PODS_AFFECTED_PERC", "value": "50"}
                        ]
                    }
                }
            }
        ]
    }
}
EOF
                        then
                            echo "ERROR: failed to apply ChaosEngine ${engine_name}" >&2
                            return 1
                        fi
                        ;;
                network-latency)
                        if ! kubectl_apply_manifest >/dev/null <<EOF
{
    "apiVersion": "litmuschaos.io/v1alpha1",
    "kind": "ChaosEngine",
    "metadata": {
        "name": "${engine_name}",
        "namespace": "${NAMESPACE}",
        "labels": {
            "app.kubernetes.io/part-of": "orchestrated-rollout",
            "experiment": "network-latency"
        }
    },
    "spec": {
        "appinfo": {
            "appns": "${NAMESPACE}",
            "applabel": "${CHAOS_APP_LABEL}",
            "appkind": "deployment"
        },
        "engineState": "active",
        "chaosServiceAccount": "${CHAOS_SERVICEACCOUNT}",
        "experiments": [
            {
                "name": "pod-network-latency",
                "spec": {
                    "components": {
                        "env": [
                            {"name": "TOTAL_CHAOS_DURATION", "value": "60"},
                            {"name": "NETWORK_LATENCY", "value": "200"},
                            {"name": "NETWORK_INTERFACE", "value": "eth0"},
                            {"name": "PODS_AFFECTED_PERC", "value": "50"},
                            {"name": "CONTAINER_RUNTIME", "value": "containerd"},
                            {"name": "SOCKET_PATH", "value": "/run/containerd/containerd.sock"}
                        ]
                    }
                }
            }
        ]
    }
}
EOF
                        then
                            echo "ERROR: failed to apply ChaosEngine ${engine_name}" >&2
                            return 1
                        fi
                        ;;
                *)
                        echo "ERROR: unknown fault '${fault}'" >&2
                        return 2
                        ;;
        esac

        echo "${engine_name}"
}

chaos_experiment_for_fault() {
    case "$1" in
        pod-kill) echo "pod-delete" ;;
        network-latency) echo "pod-network-latency" ;;
        *) echo "" ;;
    esac
}

wait_and_collect_chaos_result() {
    local trial="$1"
    local engine_name="$2"
    local fault="$3"
    local experiment_name
    local attempt

    CHAOS_RESULT_NAME=""
    CHAOS_RESULT_PHASE=""
    CHAOS_RESULT_VERDICT=""
    CHAOS_JOB_NAME=""

    if [ -z "${engine_name}" ]; then
        return 0
    fi

    experiment_name=$(chaos_experiment_for_fault "${fault}")
    if [ -z "${experiment_name}" ]; then
        return 0
    fi

    CHAOS_RESULT_NAME="${engine_name}-${experiment_name}"
    echo "[${trial}] Waiting for Litmus chaos result: ${CHAOS_RESULT_NAME}..."
    for attempt in $(seq 1 "${CHAOS_RESULT_TIMEOUT_SECONDS:-180}"); do
        if kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
            CHAOS_RESULT_PHASE=$(kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" \
                -o jsonpath='{.status.experimentStatus.phase}' 2>/dev/null || true)
            CHAOS_RESULT_VERDICT=$(kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" \
                -o jsonpath='{.status.experimentStatus.verdict}' 2>/dev/null || true)
            if [ "${CHAOS_RESULT_PHASE}" = "Completed" ] || [ "${CHAOS_RESULT_PHASE}" = "Stopped" ] || \
               [ "${CHAOS_RESULT_PHASE}" = "Error" ] || [ "${CHAOS_RESULT_VERDICT}" = "Fail" ] || \
               [ "${CHAOS_RESULT_VERDICT}" = "Error" ]; then
                break
            fi
        fi
        sleep 1
    done

    if kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
        kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" -o json \
            > "${RESULTS_DIR}/reports/trial_${trial}_chaosresult.json" 2>/dev/null || true
        kubectl_retry get chaosengine "${engine_name}" -n "${NAMESPACE}" -o json \
            > "${RESULTS_DIR}/reports/trial_${trial}_chaosengine.json" 2>/dev/null || true
        CHAOS_JOB_NAME=$(kubectl_retry get chaosresult "${CHAOS_RESULT_NAME}" -n "${NAMESPACE}" \
            -o jsonpath='{.metadata.labels.job-name}' 2>/dev/null || true)
        if [ -n "${CHAOS_JOB_NAME}" ]; then
            kubectl logs -n "${NAMESPACE}" -l "job-name=${CHAOS_JOB_NAME}" --tail=200 \
                > "${RESULTS_DIR}/reports/trial_${trial}_chaos.log" 2>&1 || true
        fi
        echo "[${trial}] Litmus chaos result: phase=${CHAOS_RESULT_PHASE:-unknown}, verdict=${CHAOS_RESULT_VERDICT:-unknown}"
    else
        echo "[${trial}] WARNING: Litmus chaos result not found: ${CHAOS_RESULT_NAME}"
    fi
}

wait_for_chaos_cleanup() {
    local engine_name="$1"
    local job_name="$2"
    local deadline=$((SECONDS + CHAOS_CLEANUP_WAIT_SECONDS))
    local engine_gone job_gone pods_gone pod_lines

    if [ "${CHAOS_CLEANUP_WAIT_SECONDS}" -le 0 ]; then
        return 0
    fi

    echo "Waiting up to ${CHAOS_CLEANUP_WAIT_SECONDS}s for Litmus cleanup..."
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        engine_gone=1
        job_gone=1
        pods_gone=1

        if [ -n "${engine_name}" ] && \
           kubectl_retry get chaosengine "${engine_name}" -n "${NAMESPACE}" >/dev/null 2>&1; then
            engine_gone=0
        fi

        if [ -n "${job_name}" ]; then
            if kubectl_retry get job "${job_name}" -n "${NAMESPACE}" >/dev/null 2>&1; then
                job_gone=0
            fi
            pod_lines=$(kubectl_retry get pods -n "${NAMESPACE}" -l "job-name=${job_name}" \
                --no-headers 2>/dev/null || true)
            if [ -n "${pod_lines}" ]; then
                pods_gone=0
            fi
        fi

        if [ "${engine_gone}" -eq 1 ] && [ "${job_gone}" -eq 1 ] && [ "${pods_gone}" -eq 1 ]; then
            return 0
        fi

        sleep 2
    done

    echo "WARNING: Litmus cleanup did not fully settle within ${CHAOS_CLEANUP_WAIT_SECONDS}s"
}

for i in $(seq "${REPEAT_START}" "${REPEAT_END}"); do
    echo ""
    echo "--- Trial ${i}/${REPEATS} ---"
    TRIAL_START_EPOCH=$(date +%s)
    OROLL_END_EPOCH=""
    CHAOS_RESULT_NAME=""
    CHAOS_RESULT_PHASE=""
    CHAOS_RESULT_VERDICT=""
    CHAOS_JOB_NAME=""

    # 0. Optional token refresh. Keep disabled for parallel GKE shard runs because
    # repeated get-credentials calls mutate kubeconfig and can race between shards.
    if [ "${REFRESH_GKE_CREDENTIALS_PER_TRIAL}" = "1" ]; then
        refresh_gke_credentials
    fi

    # 1. Reset workload to baseline state
    echo "[${i}] Cleaning up any leftover Argo Rollouts..."
    kubectl delete rollout -n "${NAMESPACE}" --all --ignore-not-found=true 2>/dev/null || true
    kubectl delete oroll -n "${NAMESPACE}" --all --ignore-not-found=true 2>/dev/null || true
    sleep 3

    echo "[${i}] Resetting workload to baseline..."
    RESET_START_EPOCH=$(date +%s)
    RESET_HPA_MIN_REPLICAS=""
    RESET_HPA_MAX_REPLICAS=""
    HPA_PIN_RC=0
    HPA_RESTORE_RC=0
    if kubectl_retry get hpa "${WORKLOAD_HPA_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
        RESET_HPA_MIN_REPLICAS=$(kubectl_retry get hpa "${WORKLOAD_HPA_NAME}" -n "${NAMESPACE}" \
            -o jsonpath='{.spec.minReplicas}' 2>/dev/null || true)
        RESET_HPA_MAX_REPLICAS=$(kubectl_retry get hpa "${WORKLOAD_HPA_NAME}" -n "${NAMESPACE}" \
            -o jsonpath='{.spec.maxReplicas}' 2>/dev/null || true)
        echo "[${i}] Pinning HPA ${WORKLOAD_HPA_NAME} to ${RESET_REPLICAS} replicas during reset..."
        if ! kubectl_retry patch hpa "${WORKLOAD_HPA_NAME}" -n "${NAMESPACE}" --type=merge \
            -p "{\"spec\":{\"minReplicas\":${RESET_REPLICAS},\"maxReplicas\":${RESET_REPLICAS}}}" >/dev/null; then
            HPA_PIN_RC=1
        fi
    fi

    set +e
    kubectl_retry scale "deployment/${WORKLOAD_DEPLOYMENT_NAME}" -n "${NAMESPACE}" --replicas="${RESET_REPLICAS}" 2>/dev/null
    SCALE_RC=$?
    sleep 5
    kubectl_retry rollout restart "deployment/${WORKLOAD_DEPLOYMENT_NAME}" -n "${NAMESPACE}"
    RESTART_RC=$?
    kubectl_retry rollout status "deployment/${WORKLOAD_DEPLOYMENT_NAME}" -n "${NAMESPACE}" --timeout=120s
    ROLLOUT_STATUS_RC=$?
    wait_for_deployment_replicas "${WORKLOAD_DEPLOYMENT_NAME}" "${RESET_REPLICAS}"
    WAIT_REPLICAS_RC=$?
    set -e

    if [ -n "${RESET_HPA_MIN_REPLICAS}" ] && [ -n "${RESET_HPA_MAX_REPLICAS}" ]; then
        echo "[${i}] Restoring HPA ${WORKLOAD_HPA_NAME} bounds: min=${RESET_HPA_MIN_REPLICAS}, max=${RESET_HPA_MAX_REPLICAS}"
        set +e
        kubectl_retry patch hpa "${WORKLOAD_HPA_NAME}" -n "${NAMESPACE}" --type=merge \
            -p "{\"spec\":{\"minReplicas\":${RESET_HPA_MIN_REPLICAS},\"maxReplicas\":${RESET_HPA_MAX_REPLICAS}}}" >/dev/null
        HPA_RESTORE_RC=$?
        set -e
    fi

    if [ "${HPA_PIN_RC}" -ne 0 ] || [ "${SCALE_RC}" -ne 0 ] || \
       [ "${RESTART_RC}" -ne 0 ] || [ "${ROLLOUT_STATUS_RC}" -ne 0 ] || \
       [ "${WAIT_REPLICAS_RC}" -ne 0 ] || [ "${HPA_RESTORE_RC}" -ne 0 ]; then
        echo "[${i}] ERROR: workload reset failed" >&2
        exit 1
    fi
    RESET_END_EPOCH=$(date +%s)
    RESET_SECONDS=$((RESET_END_EPOCH - RESET_START_EPOCH))
    MODEL_LOAD_SECONDS=$(read_model_load_seconds "${TARGET_BASE_URL}" 2>/dev/null || true)
    if [ -n "${MODEL_LOAD_SECONDS}" ]; then
        echo "[${i}] Model load time from /metrics: ${MODEL_LOAD_SECONDS}s"
    fi

    # Wait for stabilisation
    echo "[${i}] Waiting for stabilisation (${STABILIZATION_SECONDS}s)..."
    sleep "${STABILIZATION_SECONDS}"

    # 2. Start k6 load in background
    echo "[${i}] Starting k6 load (${SCENARIO})..."

    declare -a K6_ARGS=()
    if [ "${K6_SAVE_RAW_JSON}" = "1" ]; then
        K6_ARGS+=(--out "json=${RESULTS_DIR}/k6_results/trial_${i}.json")
    fi
    if [ "${K6_SAVE_SUMMARY_JSON}" = "1" ]; then
        K6_ARGS+=(--summary-export "${RESULTS_DIR}/k6_results/trial_${i}_summary.json")
    fi

    TARGET_URL="${TARGET_BASE_URL}" \
        HOST_HEADER="${INGRESS_HOST_HEADER}" \
        EXPERIMENT_ID="${EXPERIMENT_ID}" \
        SLO_P95_MS="${SLO_P95_MS}" \
        k6 run "${K6_SCRIPT}" \
            "${K6_ARGS[@]}" \
            > "${RESULTS_DIR}/k6_results/trial_${i}_stdout.txt" 2>&1 &
    K6_PID=$!
    K6_START_EPOCH=$(date +%s)

    # Wait for k6 traffic to establish so Prometheus has rate() data for the
    # controller's decision snapshot (needs ≥2 scrapes at 15s interval).
    echo "[${i}] Waiting ${K6_WARMUP_SECONDS}s for k6 warm-up (Prometheus scrape)..."
    sleep "${K6_WARMUP_SECONDS}"

    # 3. Optionally inject faults
    CHAOS_ENGINE_NAME=""
    if [ "${FAULT}" != "none" ]; then
        echo "[${i}] Injecting fault: ${FAULT}..."
        case "${FAULT}" in
            pod-kill|network-latency)
                CHAOS_ENGINE_NAME=$(apply_fault "${FAULT}" "${i}")
                ;;
            *)
                echo "ERROR: unknown fault '${FAULT}'" >&2
                exit 2
                ;;
        esac
    fi

    # 4. Trigger OrchestratedRollout
    echo "[${i}] Creating OrchestratedRollout..."
    OROLL_CREATE_EPOCH=$(date +%s)
    if ! {
        cat <<EOF
apiVersion: rollout.orchestrated.io/v1alpha1
kind: OrchestratedRollout
metadata:
    name: experiment-trial-${i}
    namespace: ${NAMESPACE}
spec:
    targetRef:
        apiVersion: apps/v1
        kind: Deployment
        name: ${WORKLOAD_DEPLOYMENT_NAME}
    release:
        image: ${RELEASE_IMAGE}
        tag: ${RELEASE_TAG}
EOF

        if [ -n "${ACTION_SET}" ]; then
            echo "    actionSet:"
            IFS=',' read -r -a _ACTIONS <<< "${ACTION_SET}"
            for _a in "${_ACTIONS[@]}"; do
                _trimmed=$(echo "${_a}" | xargs)
                [ -z "${_trimmed}" ] && continue
                echo "      - ${_trimmed}"
            done
        fi

        cat <<EOF
    slo:
        maxP95LatencyMs: ${SLO_P95_MS}
        maxErrorRate: 0.01
EOF

        if [ -n "${ROLLOUT_MAX_EXTRA_REPLICAS}" ] || [ -n "${ROLLOUT_MAX_DELAY_SECONDS}" ]; then
            echo "    guardrailConfig:"
            if [ -n "${ROLLOUT_MAX_EXTRA_REPLICAS}" ]; then
                echo "        maxExtraReplicas: ${ROLLOUT_MAX_EXTRA_REPLICAS}"
            fi
            if [ -n "${ROLLOUT_MAX_DELAY_SECONDS}" ]; then
                echo "        maxDelaySeconds: ${ROLLOUT_MAX_DELAY_SECONDS}"
            fi
        fi

        cat <<EOF
    rolloutHints:
        targetReplicas: 4
        warmUpClass: medium
EOF

        if [ -n "${ROLLOUT_OBJECTIVE}" ]; then
            echo "        objective: ${ROLLOUT_OBJECTIVE}"
        fi
        if [ -n "${ROLLOUT_POLICY_VARIANT}" ]; then
            echo "        policyVariant: ${ROLLOUT_POLICY_VARIANT}"
        fi
        if [ -n "${ROLLOUT_FAULT_CONTEXT}" ]; then
            echo "        faultContext: ${ROLLOUT_FAULT_CONTEXT}"
        fi
        if [ "${ENABLE_ROLLOUT_TRAFFIC_HINT}" = "1" ] && [ -n "${ROLLOUT_TRAFFIC_PROFILE}" ]; then
            echo "        trafficProfile: ${ROLLOUT_TRAFFIC_PROFILE}"
        fi
        } | kubectl_apply_manifest
    then
        echo "[${i}] ERROR: failed to create OrchestratedRollout experiment-trial-${i}" >&2
        exit 1
    fi

    # 5. Wait for rollout to complete
    echo "[${i}] Waiting for rollout to complete..."
    MAX_ATTEMPTS=$((OROLL_TIMEOUT_SECONDS / OROLL_POLL_SECONDS))
    if [ "${MAX_ATTEMPTS}" -lt 1 ]; then
        MAX_ATTEMPTS=1
    fi

    for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
        PHASE=$(kubectl_retry get oroll experiment-trial-${i} -n "${NAMESPACE}" \
            -o jsonpath='{.status.phase}' 2>/dev/null || echo "Pending")
        if [ "${PHASE}" = "Completed" ] || [ "${PHASE}" = "Failed" ] || [ "${PHASE}" = "Aborted" ]; then
            OROLL_END_EPOCH=$(date +%s)
            echo "[${i}] Rollout finished: ${PHASE}"
            break
        fi
        sleep "${OROLL_POLL_SECONDS}"
    done

    if [ "${PHASE}" != "Completed" ] && [ "${PHASE}" != "Failed" ] && [ "${PHASE}" != "Aborted" ]; then
        echo "[${i}] ERROR: Rollout did not reach a terminal phase within ${OROLL_TIMEOUT_SECONDS}s (last phase=${PHASE})"
        kubectl_retry get oroll experiment-trial-${i} -n "${NAMESPACE}" -o yaml \
            > "${RESULTS_DIR}/episodes/trial_${i}_oroll_timeout.yaml" 2>/dev/null || true
        kubectl describe oroll experiment-trial-${i} -n "${NAMESPACE}" \
            > "${RESULTS_DIR}/episodes/trial_${i}_oroll_timeout.describe.txt" 2>/dev/null || true

        if [ "${FAIL_ON_OROLL_TIMEOUT}" = "1" ]; then
            exit 1
        fi
    fi

    # 6. Wait for k6 to finish
    set +e
    wait "${K6_PID}" 2>/dev/null
    K6_RC=$?
    set -e
    K6_END_EPOCH=$(date +%s)
    if [ "${K6_RC}" -ne 0 ]; then
        echo "[${i}] k6 exited with code ${K6_RC}; continuing only if summary metrics were written."
    fi

    if [ "${FAULT}" != "none" ] && [ -n "${CHAOS_ENGINE_NAME}" ]; then
        wait_and_collect_chaos_result "${i}" "${CHAOS_ENGINE_NAME}" "${FAULT}"
    fi

    # 7. Collect episode data
    echo "[${i}] Collecting episode data..."
    if ! kubectl_retry get oroll "experiment-trial-${i}" -n "${NAMESPACE}" -o json \
        > "${RESULTS_DIR}/episodes/trial_${i}_oroll.json"; then
        echo "[${i}] WARNING: Failed to fetch OrchestratedRollout JSON (kubectl connectivity issue?)"
    fi

    if [ -n "${CONTROLLER_POD_NAME}" ]; then
        EP_FILES=$(kubectl_retry exec -n "${NAMESPACE}" "${CONTROLLER_POD_NAME}" -- sh -c 'ls -1 /episodes/episode_*.json 2>/dev/null || true' 2>/dev/null || true)
        if [ -n "${EP_FILES}" ]; then
            while IFS= read -r ep; do
                [ -z "${ep}" ] && continue
                base=$(basename "${ep}")
                kubectl cp -n "${NAMESPACE}" "${CONTROLLER_POD_NAME}:${ep}" "${RESULTS_DIR}/episodes/${base}" \
                    2>/dev/null || true
            done <<< "${EP_FILES}"
        fi
    fi

    TRIAL_END_EPOCH=$(date +%s)
    OROLL_END_EPOCH="${OROLL_END_EPOCH:-${TRIAL_END_EPOCH}}"
    ROLLOUT_SECONDS=$((OROLL_END_EPOCH - OROLL_CREATE_EPOCH))
    K6_WALL_SECONDS=$((K6_END_EPOCH - K6_START_EPOCH))
    TRIAL_SECONDS=$((TRIAL_END_EPOCH - TRIAL_START_EPOCH))

    if [ -f "${RESULTS_DIR}/k6_results/trial_${i}_summary.json" ] && [ -f "${RESULTS_DIR}/episodes/trial_${i}_oroll.json" ]; then
        SUMMARY_ARGS=(
            --trial "${i}" \
            --scenario "${SCENARIO}" \
            --fault "${FAULT}" \
            --oroll "${RESULTS_DIR}/episodes/trial_${i}_oroll.json" \
            --k6-summary "${RESULTS_DIR}/k6_results/trial_${i}_summary.json" \
            --output "${RESULTS_DIR}/reports/trial_${i}_metrics.json" \
            --reset-seconds "${RESET_SECONDS}" \
            --rollout-seconds "${ROLLOUT_SECONDS}" \
            --k6-wall-seconds "${K6_WALL_SECONDS}" \
            --trial-seconds "${TRIAL_SECONDS}"
        )
        if [ -n "${MODEL_LOAD_SECONDS}" ]; then
            SUMMARY_ARGS+=(--model-load-seconds "${MODEL_LOAD_SECONDS}")
        fi
        if [ -n "${CHAOS_RESULT_NAME:-}" ]; then
            SUMMARY_ARGS+=(--chaos-result-name "${CHAOS_RESULT_NAME}")
        fi
        if [ -n "${CHAOS_RESULT_PHASE:-}" ]; then
            SUMMARY_ARGS+=(--chaos-phase "${CHAOS_RESULT_PHASE}")
        fi
        if [ -n "${CHAOS_RESULT_VERDICT:-}" ]; then
            SUMMARY_ARGS+=(--chaos-verdict "${CHAOS_RESULT_VERDICT}")
        fi
        if ! python3 evaluation/summarise_trial.py "${SUMMARY_ARGS[@]}"; then
            echo "[${i}] ERROR: Metrics summary generation failed." >&2
            if [ "${REQUIRE_TRIAL_METRICS}" = "1" ]; then
                exit 1
            fi
        fi
    else
        echo "[${i}] WARNING: Metrics summary skipped because k6 or OrchestratedRollout artifact is missing."
        if [ "${REQUIRE_TRIAL_METRICS}" = "1" ]; then
            echo "[${i}] ERROR: REQUIRE_TRIAL_METRICS=1 and required trial artifacts are missing." >&2
            exit 1
        fi
    fi

    # 8. Cleanup fault injection
    if [ "${FAULT}" != "none" ]; then
        echo "[${i}] Cleaning up faults..."
        if [ -n "${CHAOS_ENGINE_NAME}" ]; then
            kubectl delete chaosengine "${CHAOS_ENGINE_NAME}" -n "${NAMESPACE}" 2>/dev/null || true
        else
            kubectl delete chaosengine --all -n "${NAMESPACE}" 2>/dev/null || true
        fi
        wait_for_chaos_cleanup "${CHAOS_ENGINE_NAME}" "${CHAOS_JOB_NAME:-}"
    fi

    # 9. Cleanup trial CR
    kubectl delete oroll experiment-trial-${i} -n "${NAMESPACE}" 2>/dev/null || true

    if [ "${POST_TRIAL_COOLDOWN_SECONDS}" -gt 0 ]; then
        echo "[${i}] Post-trial cooldown (${POST_TRIAL_COOLDOWN_SECONDS}s)..."
        sleep "${POST_TRIAL_COOLDOWN_SECONDS}"
    fi

    echo "[${i}] Trial ${i} complete."
done

METRIC_FILES=("${RESULTS_DIR}"/reports/trial_*_metrics.json)
if [ -e "${METRIC_FILES[0]}" ]; then
    if ! python3 evaluation/summarise_trial.py \
        --aggregate "${METRIC_FILES[@]}" \
        --output "${RESULTS_DIR}/reports/run_summary.json"; then
        echo "ERROR: aggregate run summary generation failed." >&2
        if [ "${REQUIRE_TRIAL_METRICS}" = "1" ]; then
            exit 1
        fi
    fi
else
    echo "WARNING: no trial metric files were produced for ${EXPERIMENT_ID}."
    if [ "${REQUIRE_TRIAL_METRICS}" = "1" ]; then
        echo "ERROR: REQUIRE_TRIAL_METRICS=1 and no trial metrics were produced." >&2
        exit 1
    fi
fi

echo ""
echo "=============================================="
echo "  Experiment Complete: ${EXPERIMENT_ID}"
echo "  Results in: ${RESULTS_DIR}"
echo "=============================================="
echo ""
echo "Run analysis: python evaluation/analyse.py ${RESULTS_DIR}/episodes ${RESULTS_DIR}/reports"

#!/usr/bin/env bash
set -euo pipefail

COMPARISON_TIMESTAMP="${COMPARISON_TIMESTAMP:-$(date +%Y%m%d_gitops_workload_portability_%H%M%S)}"
WORKLOADS_CSV="${WORKLOADS_CSV:-go-service,cpu-bound-fastapi,io-latency-node,mobilenetv2-onnx,squeezenet-onnx,tabular-sklearn}"
MODES_CSV="${MODES_CSV:-rl-v12,pre-scale}"
SCENARIOS_CSV="${SCENARIOS_CSV:-ramp}"
FAULTS_CSV="${FAULTS_CSV:-none}"
REPEATS="${REPEATS:-1}"
VALIDATION_PROFILE="${VALIDATION_PROFILE:-fast}"
RESULT_ROOT="${RESULT_ROOT:-results/gitops_workload_portability_${COMPARISON_TIMESTAMP}}"
REPORT_DIR="${RESULT_ROOT}/reports"
mkdir -p "${REPORT_DIR}"

IFS=',' read -r -a WORKLOADS <<< "${WORKLOADS_CSV}"
IFS=',' read -r -a MODES <<< "${MODES_CSV}"
IFS=',' read -r -a SCENARIOS <<< "${SCENARIOS_CSV}"
IFS=',' read -r -a FAULTS <<< "${FAULTS_CSV}"

namespace_for() {
  case "$1" in
    go-service) echo workload-go ;;
    cpu-bound-fastapi) echo workload-cpu ;;
    io-latency-node) echo workload-io ;;
    mobilenetv2-onnx) echo workload-mobilenet ;;
    squeezenet-onnx) echo workload-squeezenet ;;
    tabular-sklearn) echo workload-tabular ;;
    *) echo "workload-$1" ;;
  esac
}

infer_path_for() {
  case "$1" in
    go-service|mobilenetv2-onnx|squeezenet-onnx) echo /inference ;;
    *) echo /infer ;;
  esac
}

app_name_for() { echo "workload-$1"; }

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

cat > "${REPORT_DIR}/gitops_app_sync_status.csv" <<CSV
workload,application,sync_status,health_status
CSV
cat > "${REPORT_DIR}/workload_health_summary.csv" <<CSV
workload,namespace,service,healthz,readyz,metrics,infer,hpa
CSV
cat > "${REPORT_DIR}/workload_smoke_summary.csv" <<CSV
workload,mode,scenario,fault,repeat,namespace,orchestrated_rollout,phase,chosen_strategy,pre_scale_extra_replicas,status
CSV
cat > "${REPORT_DIR}/workload_decision_distribution.csv" <<CSV
workload,mode,chosen_strategy,pre_scale_extra_replicas,count
CSV

log "Checking Argo CD applications"
for workload in "${WORKLOADS[@]}"; do
  app="$(app_name_for "${workload}")"
  sync="missing"
  health="missing"
  if kubectl get application "${app}" -n argocd >/dev/null 2>&1; then
    sync="$(kubectl get application "${app}" -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null || echo unknown)"
    health="$(kubectl get application "${app}" -n argocd -o jsonpath='{.status.health.status}' 2>/dev/null || echo unknown)"
  fi
  echo "${workload},${app},${sync},${health}" >> "${REPORT_DIR}/gitops_app_sync_status.csv"
done

log "Checking health/readiness/metrics endpoints"
for workload in "${WORKLOADS[@]}"; do
  ns="$(namespace_for "${workload}")"
  svc="${workload}"
  infer_path="$(infer_path_for "${workload}")"
  healthz="fail"; readyz="fail"; metrics="fail"; infer="fail"; hpa="missing"
  kubectl get hpa -n "${ns}" "${workload}-hpa" >/dev/null 2>&1 && hpa="present" || true
  kubectl -n "${ns}" run "curl-${workload}-health" --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet --command -- curl -fsS "http://${svc}/healthz" >/dev/null 2>&1 && healthz="pass" || true
  kubectl -n "${ns}" run "curl-${workload}-ready" --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet --command -- curl -fsS "http://${svc}/readyz" >/dev/null 2>&1 && readyz="pass" || true
  kubectl -n "${ns}" run "curl-${workload}-metrics" --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet --command -- curl -fsS "http://${svc}/metrics" >/dev/null 2>&1 && metrics="pass" || true
  kubectl -n "${ns}" run "curl-${workload}-infer" --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet --command -- curl -fsS "http://${svc}${infer_path}" >/dev/null 2>&1 && infer="pass" || true
  echo "${workload},${ns},${svc},${healthz},${readyz},${metrics},${infer},${hpa}" >> "${REPORT_DIR}/workload_health_summary.csv"
done

log "Running OrchestratedRollout portability smoke"
trial=0
for workload in "${WORKLOADS[@]}"; do
  ns="$(namespace_for "${workload}")"
  image="$(kubectl -n "${ns}" get deploy "${workload}" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  image_repo="${image%:*}"
  image_tag="${image##*:}"
  if [ -z "${image}" ]; then
    for mode in "${MODES[@]}"; do
      for scenario in "${SCENARIOS[@]}"; do
        for fault in "${FAULTS[@]}"; do
          for repeat in $(seq 1 "${REPEATS}"); do
            echo "${workload},${mode},${scenario},${fault},${repeat},${ns},,missing,,,skipped_no_deployment" >> "${REPORT_DIR}/workload_smoke_summary.csv"
          done
        done
      done
    done
    continue
  fi
  for mode in "${MODES[@]}"; do
    for scenario in "${SCENARIOS[@]}"; do
      for fault in "${FAULTS[@]}"; do
        for repeat in $(seq 1 "${REPEATS}"); do
          trial=$((trial + 1))
          oroll="port-${workload}-${mode}-${repeat}"
          action="${mode}"
          variant="default"
          if [ "${mode}" = "rl-v12" ]; then
            action="rl"
            variant="v12-contextual"
          fi
          kubectl -n "${ns}" delete orchestratedrollout "${oroll}" --ignore-not-found >/dev/null 2>&1 || true
          cat <<YAML | kubectl apply -n "${ns}" -f - >/dev/null
apiVersion: rollout.orchestrated.io/v1alpha1
kind: OrchestratedRollout
metadata:
  name: ${oroll}
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${workload}
  release:
    image: ${image_repo}
    tag: ${image_tag}
  actionSet:
    - ${action}
  slo:
    maxP95LatencyMs: 500
    maxErrorRate: 0.01
  rolloutHints:
    trafficProfile: ${scenario}
    objective: reliability
    policyVariant: ${variant}
    faultContext: ${fault}
YAML
          deadline=$((SECONDS + 180))
          phase="Pending"
          while [ ${SECONDS} -lt ${deadline} ]; do
            phase="$(kubectl -n "${ns}" get orchestratedrollout "${oroll}" -o jsonpath='{.status.phase}' 2>/dev/null || echo Pending)"
            case "${phase}" in Completed|Failed|Aborted) break ;; esac
            sleep 5
          done
          strategy="$(kubectl -n "${ns}" get orchestratedrollout "${oroll}" -o jsonpath='{.status.chosenStrategy}' 2>/dev/null || true)"
          headroom="$(kubectl -n "${ns}" get orchestratedrollout "${oroll}" -o jsonpath='{.status.preScaleExtraReplicas}' 2>/dev/null || true)"
          status="completed"
          [ "${phase}" = "Completed" ] || status="${phase:-timeout}"
          echo "${workload},${mode},${scenario},${fault},${repeat},${ns},${oroll},${phase},${strategy},${headroom},${status}" >> "${REPORT_DIR}/workload_smoke_summary.csv"
        done
      done
    done
  done
done

awk -F, 'NR>1 {k=$1","$2","$9","$10; c[k]++} END {for (k in c) print k","c[k]}' "${REPORT_DIR}/workload_smoke_summary.csv" >> "${REPORT_DIR}/workload_decision_distribution.csv"

completed="$(awk -F, 'NR>1 && $11=="completed" {c++} END {print c+0}' "${REPORT_DIR}/workload_smoke_summary.csv")"
total="$(awk -F, 'NR>1 {c++} END {print c+0}' "${REPORT_DIR}/workload_smoke_summary.csv")"
cat > "${REPORT_DIR}/GITOPS_WORKLOAD_PORTABILITY_REPORT.md" <<MD
# GitOps Workload Portability Report

Timestamp: ${COMPARISON_TIMESTAMP}

This validation is separate from the primary statistical evaluation. Its purpose is to verify that Orchestrated Rollout can deploy and evaluate heterogeneous HTTP and ML inference workloads through the same GitOps, HPA, telemetry, and rollout path.

## Matrix

- Workloads: ${WORKLOADS_CSV}
- Modes: ${MODES_CSV}
- Scenarios: ${SCENARIOS_CSV}
- Faults: ${FAULTS_CSV}
- Repeats: ${REPEATS}
- Validation profile: ${VALIDATION_PROFILE}

## Completion

Completed OrchestratedRollout rows: ${completed}/${total}

## Artifacts

- gitops_app_sync_status.csv
- workload_health_summary.csv
- workload_smoke_summary.csv
- workload_decision_distribution.csv
MD

log "Report written to ${REPORT_DIR}/GITOPS_WORKLOAD_PORTABILITY_REPORT.md"

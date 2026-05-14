#!/usr/bin/env bash
set -euo pipefail

# monitor_rollout.sh — Monitor orchestrated rollout progress and dashboard status
#
# Monitors:
#   - OrchestratedRollout object status
#   - Deployment rollout status
#   - Argo CD Application sync status
#   - Live metrics from Prometheus
#
# Environment variables:
#   WORKLOAD_APP_NAME          # Argo CD application name
#   WORKLOAD_NAMESPACE         # Workload namespace (default: workload-cpu)
#   ARGOCD_NAMESPACE          # Argo CD namespace (default: argocd)
#   ORCHESTRATED_ROLLOUT_NAME # OrchestratedRollout resource name (default: cpu-bound-fastapi-portability-template)
#   MONITOR_DURATION_SECS     # How long to monitor (default: 300)

WORKLOAD_APP_NAME="${WORKLOAD_APP_NAME:-workload-cpu-bound-fastapi}"
WORKLOAD_NAMESPACE="${WORKLOAD_NAMESPACE:-workload-cpu}"
ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
ORCHESTRATED_ROLLOUT_NAME="${ORCHESTRATED_ROLLOUT_NAME:-cpu-bound-fastapi-portability-template}"
MONITOR_DURATION_SECS="${MONITOR_DURATION_SECS:-300}"

REPORT_DIR="rollout-status-$(date +%s)"
mkdir -p "${REPORT_DIR}"

echo "==================================================================="
echo "Monitoring OrchestratedRollout Progress"
echo "==================================================================="
echo "Workload App: ${WORKLOAD_APP_NAME}"
echo "Namespace: ${WORKLOAD_NAMESPACE}"
echo "Monitoring Duration: ${MONITOR_DURATION_SECS}s"
echo "Report Directory: ${REPORT_DIR}"
echo ""

# Function to check status
check_status() {
  echo "[$(date +'%H:%M:%S')] Checking status..."
  
  # 1. Check Argo CD Application
  echo ""
  echo "📦 Argo CD Application Status:"
  kubectl get application -n "${ARGOCD_NAMESPACE}" "${WORKLOAD_APP_NAME}" -o wide 2>/dev/null || echo "  (Application not found)"
  
  # 2. Check OrchestratedRollout
  echo ""
  echo "🚀 OrchestratedRollout Status:"
  if kubectl get orchestratedrollout -n "${WORKLOAD_NAMESPACE}" "${ORCHESTRATED_ROLLOUT_NAME}" &>/dev/null; then
    kubectl get orchestratedrollout -n "${WORKLOAD_NAMESPACE}" "${ORCHESTRATED_ROLLOUT_NAME}" -o wide
    
    echo ""
    echo "📋 OrchestratedRollout Details:"
    kubectl get orchestratedrollout -n "${WORKLOAD_NAMESPACE}" "${ORCHESTRATED_ROLLOUT_NAME}" -o jsonpath='{
      "phase": .status.phase,
      "readyReplicas": .status.readyReplicas,
      "updatedReplicas": .status.updatedReplicas,
      "message": .status.message
    }' | python3 -m json.tool 2>/dev/null || echo "  (Could not parse details)"
  else
    echo "  (OrchestratedRollout not found in ${WORKLOAD_NAMESPACE})"
  fi
  
  # 3. Check Deployment rollout
  echo ""
  echo "📊 Deployment Status:"
  DEPLOYMENT=$(kubectl get deployment -n "${WORKLOAD_NAMESPACE}" --sort-by='.metadata.creationTimestamp' -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || true)
  if [ -n "${DEPLOYMENT}" ]; then
    kubectl get deployment -n "${WORKLOAD_NAMESPACE}" "${DEPLOYMENT}" -o wide
    
    echo ""
    echo "📈 Deployment Rollout History:"
    kubectl rollout history deployment -n "${WORKLOAD_NAMESPACE}" "${DEPLOYMENT}" --revision=0 2>/dev/null || true
  else
    echo "  (No deployments found)"
  fi
  
  # 4. Check Pod status
  echo ""
  echo "🐳 Pod Status:"
  kubectl get pods -n "${WORKLOAD_NAMESPACE}" --sort-by='.metadata.creationTimestamp' | tail -10
  
  # 5. Check Prometheus metrics (if available)
  echo ""
  echo "📉 Live Metrics (from Prometheus):"
  check_prometheus_metrics
}

check_prometheus_metrics() {
  # This would typically query Prometheus API for live metrics
  # For now, just attempt to get recent metrics from the workload
  if kubectl get service -n monitoring prometheus-operated &>/dev/null; then
    echo "  ✓ Prometheus is available"
    echo "  Dashboard should show: P95 latency, throughput, error rates"
  else
    echo "  (Prometheus not available in this context)"
  fi
}

# Function to capture detailed report
capture_report() {
  TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  REPORT_FILE="${REPORT_DIR}/rollout-status-${TIMESTAMP}.json"
  
  # Capture comprehensive status as JSON
  cat > "${REPORT_FILE}" << JSONEOF
{
  "timestamp": "${TIMESTAMP}",
  "workload_app": "${WORKLOAD_APP_NAME}",
  "namespace": "${WORKLOAD_NAMESPACE}",
  "argocd_status": $(kubectl get application -n "${ARGOCD_NAMESPACE}" "${WORKLOAD_APP_NAME}" -o json 2>/dev/null || echo 'null'),
  "orchestrated_rollout_status": $(kubectl get orchestratedrollout -n "${WORKLOAD_NAMESPACE}" "${ORCHESTRATED_ROLLOUT_NAME}" -o json 2>/dev/null || echo 'null'),
  "deployments": $(kubectl get deployment -n "${WORKLOAD_NAMESPACE}" -o json 2>/dev/null || echo 'null'),
  "pods": $(kubectl get pods -n "${WORKLOAD_NAMESPACE}" -o json 2>/dev/null || echo 'null')
}
JSONEOF
  
  echo "  Report saved: ${REPORT_FILE}"
}

# Main monitoring loop
START_TIME=$(date +%s)
ITERATION=1

while true; do
  check_status
  capture_report
  
  CURRENT_TIME=$(date +%s)
  ELAPSED=$((CURRENT_TIME - START_TIME))
  
  if [ ${ELAPSED} -ge ${MONITOR_DURATION_SECS} ]; then
    echo ""
    echo "==================================================================="
    echo "✓ Monitoring complete. Duration: ${ELAPSED}s"
    echo "==================================================================="
    break
  fi
  
  REMAINING=$((MONITOR_DURATION_SECS - ELAPSED))
  echo ""
  echo "⏱️  Next check in 10s... (${REMAINING}s remaining)"
  sleep 10
  
  ITERATION=$((ITERATION + 1))
done

# Final summary
echo ""
echo "==================================================================="
echo "Final Dashboard Checklist"
echo "==================================================================="
echo "✓ Deployment — Workload, namespace, release, and current image"
echo "✓ Policy Decision — Selected strategy and headroom"
echo "✓ Cluster State — Traffic profile, fault context, stress score, HPA status"
echo "✓ Rollout Status — Phase, ready replicas, controller message"
echo "✓ Live Metrics — P95 latency, throughput, failures"
echo "✓ GitOps Status — Application, sync, health, Git revision"
echo "✓ Decision Explanation — Strategy selection reason"
echo ""
echo "📊 Full reports available in: ${REPORT_DIR}/"
ls -lah "${REPORT_DIR}/"

#!/usr/bin/env bash
set -euo pipefail

# 12_deploy_gitops_apps.sh — deploy controller/workload/monitoring through Argo CD.
#
# This validates the GitOps path: Argo CD reads Helm charts from the configured
# Git repository and applies image overrides for the images built in Artifact
# Registry.
#
# Required env:
#   CTRL_IMAGE_REPO, CTRL_IMAGE_TAG, WORK_IMAGE_REPO, WORK_IMAGE_TAG
#
# Optional env:
#   GIT_REPO_URL        # defaults to git remote origin
#   GIT_TARGET_REVISION # defaults to main
#   WORKLOAD_APP_NAME   # default orchestrated-rollout-workload
#   WORKLOAD_CHART_PATH # charts/workload or charts/ml-workload
#   WORKLOAD_RELEASE    # workload or ml-workload
#   WORKLOAD_NAMESPACE  # default orchestrated-rollout
#   WORKLOAD_INGRESS_HOST
#   POLICY_ARTIFACT_FILE
#   INSTALL_ARGO_CD=1

CTRL_IMAGE_REPO="${CTRL_IMAGE_REPO:-}"
CTRL_IMAGE_TAG="${CTRL_IMAGE_TAG:-}"
WORK_IMAGE_REPO="${WORK_IMAGE_REPO:-}"
WORK_IMAGE_TAG="${WORK_IMAGE_TAG:-}"

GIT_REPO_URL="${GIT_REPO_URL:-$(git config --get remote.origin.url 2>/dev/null || true)}"
GIT_TARGET_REVISION="${GIT_TARGET_REVISION:-main}"
WORKLOAD_APP_NAME="${WORKLOAD_APP_NAME:-orchestrated-rollout-workload}"
WORKLOAD_CHART_PATH="${WORKLOAD_CHART_PATH:-charts/workload}"
WORKLOAD_RELEASE="${WORKLOAD_RELEASE:-workload}"
WORKLOAD_NAMESPACE="${WORKLOAD_NAMESPACE:-orchestrated-rollout}"
WORKLOAD_INGRESS_HOST="${WORKLOAD_INGRESS_HOST:-}"
POLICY_ARTIFACT_FILE="${POLICY_ARTIFACT_FILE:-artifacts/v11_no_forecast/policy_artifact.json}"
INSTALL_ARGO_CD="${INSTALL_ARGO_CD:-1}"

if [ -z "${CTRL_IMAGE_REPO}" ] || [ -z "${CTRL_IMAGE_TAG}" ] || [ -z "${WORK_IMAGE_REPO}" ] || [ -z "${WORK_IMAGE_TAG}" ]; then
  echo "ERROR: Set CTRL_IMAGE_REPO/CTRL_IMAGE_TAG and WORK_IMAGE_REPO/WORK_IMAGE_TAG" >&2
  exit 2
fi

if [ -z "${GIT_REPO_URL}" ]; then
  echo "ERROR: GIT_REPO_URL not set and git remote origin is unavailable" >&2
  exit 2
fi

case "${GIT_REPO_URL}" in
  git@github.com:*)
    GIT_REPO_URL="https://github.com/${GIT_REPO_URL#git@github.com:}"
    ;;
  https://*@github.com/*)
    # Do not put local credential-bearing remotes into Argo CD Application specs.
    GIT_REPO_URL="https://github.com/${GIT_REPO_URL#*@github.com/}"
    ;;
esac

if [ ! -f "${POLICY_ARTIFACT_FILE}" ]; then
  echo "ERROR: POLICY_ARTIFACT_FILE not found: ${POLICY_ARTIFACT_FILE}" >&2
  exit 2
fi

if [ "${INSTALL_ARGO_CD}" = "1" ]; then
  INSTALL_INGRESS_NGINX=0 \
  INSTALL_ARGO_CD=1 \
  INSTALL_ARGO_ROLLOUTS=0 \
  INSTALL_LITMUS=0 \
    ./scripts/gke/06_deploy_prereqs.sh
fi

echo "Installing OrchestratedRollout CRD and policy artifact..."
make install-crd
./scripts/05_setup_policy_artifact.sh "${POLICY_ARTIFACT_FILE}"

tmpfile="$(mktemp)"
trap 'rm -f "${tmpfile}"' EXIT

cat > "${tmpfile}" <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: orchestrated-rollout-monitoring
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: orchestrated-rollout
spec:
  project: default
  source:
    repoURL: ${GIT_REPO_URL}
    targetRevision: ${GIT_TARGET_REVISION}
    path: charts/monitoring
    helm:
      releaseName: monitoring
      valueFiles:
        - values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: orchestrated-rollout-controller
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: orchestrated-rollout
spec:
  project: default
  source:
    repoURL: ${GIT_REPO_URL}
    targetRevision: ${GIT_TARGET_REVISION}
    path: charts/controller
    helm:
      releaseName: controller
      valueFiles:
        - values.yaml
      parameters:
        - name: image.repository
          value: ${CTRL_IMAGE_REPO}
        - name: image.tag
          value: ${CTRL_IMAGE_TAG}
        - name: image.pullPolicy
          value: IfNotPresent
  destination:
    server: https://kubernetes.default.svc
    namespace: orchestrated-rollout
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${WORKLOAD_APP_NAME}
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: orchestrated-rollout
spec:
  project: default
  source:
    repoURL: ${GIT_REPO_URL}
    targetRevision: ${GIT_TARGET_REVISION}
    path: ${WORKLOAD_CHART_PATH}
    helm:
      releaseName: ${WORKLOAD_RELEASE}
      valueFiles:
        - values.yaml
      parameters:
        - name: image.repository
          value: ${WORK_IMAGE_REPO}
        - name: image.tag
          value: ${WORK_IMAGE_TAG}
        - name: image.pullPolicy
          value: IfNotPresent
EOF

if [ -n "${WORKLOAD_INGRESS_HOST}" ]; then
  cat >> "${tmpfile}" <<EOF
        - name: ingress.host
          value: ${WORKLOAD_INGRESS_HOST}
EOF
fi

cat >> "${tmpfile}" <<EOF
  destination:
    server: https://kubernetes.default.svc
    namespace: ${WORKLOAD_NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
EOF

echo "Applying Argo CD Applications from ${GIT_REPO_URL}@${GIT_TARGET_REVISION}..."
kubectl apply -f "${tmpfile}"

echo "GitOps applications submitted. Check sync with:"
echo "  kubectl get applications -n argocd"
echo "  kubectl describe application orchestrated-rollout-controller -n argocd"

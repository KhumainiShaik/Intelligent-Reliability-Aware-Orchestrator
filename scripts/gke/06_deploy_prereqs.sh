#!/usr/bin/env bash
set -euo pipefail

# 06_deploy_prereqs.sh — install cluster prerequisites used by the experiments.
#
# Usage:
#   ./scripts/gke/06_deploy_prereqs.sh

# Optional env:
#   INSTALL_INGRESS_NGINX=1
#   INSTALL_ARGO_CD=1
#   INSTALL_ARGO_ROLLOUTS=1
#   INSTALL_LITMUS=1

INSTALL_INGRESS_NGINX="${INSTALL_INGRESS_NGINX:-1}"
INSTALL_ARGO_CD="${INSTALL_ARGO_CD:-1}"
INSTALL_ARGO_ROLLOUTS="${INSTALL_ARGO_ROLLOUTS:-1}"
INSTALL_LITMUS="${INSTALL_LITMUS:-1}"
CHAOS_NAMESPACE="${CHAOS_NAMESPACE:-orchestrated-rollout}"
CHAOS_SERVICEACCOUNT="${CHAOS_SERVICEACCOUNT:-oroll-litmus-admin}"
CHAOS_CLUSTERROLE="${CHAOS_CLUSTERROLE:-oroll-litmus-admin}"
LITMUS_OPERATOR_VERSION="${LITMUS_OPERATOR_VERSION:-3.28.0}"
LITMUS_OPERATOR_IMAGE="${LITMUS_OPERATOR_IMAGE:-litmuschaos.docker.scarf.sh/litmuschaos/chaos-operator:${LITMUS_OPERATOR_VERSION}}"
LITMUS_RUNNER_IMAGE="${LITMUS_RUNNER_IMAGE:-litmuschaos.docker.scarf.sh/litmuschaos/chaos-runner:${LITMUS_OPERATOR_VERSION}}"

echo "Creating namespaces..."
kubectl apply -f k8s/base/namespaces.yaml

if [ "${INSTALL_INGRESS_NGINX}" = "1" ]; then
  echo "Installing ingress-nginx (Helm)..."
  helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true
  helm repo update >/dev/null 2>&1 || true

  helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
    --namespace ingress-nginx --create-namespace \
    --set controller.metrics.enabled=true \
    --wait --timeout 10m

  echo "Waiting for ingress controller pod..."
  kubectl wait --namespace ingress-nginx \
    --for=condition=ready pod \
    --selector=app.kubernetes.io/component=controller \
    --timeout=300s
fi

if [ "${INSTALL_ARGO_CD}" = "1" ]; then
  echo "Installing Argo CD..."
  kubectl apply --server-side --force-conflicts -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
  kubectl wait -n argocd --for=condition=ready pod --all --timeout=600s
fi

if [ "${INSTALL_ARGO_ROLLOUTS}" = "1" ]; then
  echo "Installing Argo Rollouts..."
  kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
  kubectl wait -n argo-rollouts --for=condition=available deployment/argo-rollouts --timeout=300s
fi

if [ "${INSTALL_LITMUS}" = "1" ]; then
  echo "Installing LitmusChaos..."
  helm repo add litmuschaos https://litmuschaos.github.io/litmus-helm/ >/dev/null 2>&1 || true
  helm repo update >/dev/null 2>&1 || true
  helm upgrade --install litmus litmuschaos/litmus \
    --namespace litmus --create-namespace \
    --wait --timeout 15m

  if ! kubectl get crd chaosengines.litmuschaos.io >/dev/null 2>&1; then
    echo "Installing Litmus chaos CRDs..."
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/chaos_crds.yaml"
    kubectl wait --for=condition=Established crd/chaosengines.litmuschaos.io --timeout=60s
    kubectl wait --for=condition=Established crd/chaosexperiments.litmuschaos.io --timeout=60s
    kubectl wait --for=condition=Established crd/chaosresults.litmuschaos.io --timeout=60s
  fi

  if ! kubectl get deployment litmus -n litmus >/dev/null 2>&1; then
    echo "Installing Litmus chaos-operator..."
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/rbac.yaml"
    kubectl apply -f "https://raw.githubusercontent.com/litmuschaos/chaos-operator/${LITMUS_OPERATOR_VERSION}/deploy/operator.yaml"
  fi
  kubectl set image deployment/litmus -n litmus "chaos-operator=${LITMUS_OPERATOR_IMAGE}" >/dev/null
  kubectl set env deployment/litmus -n litmus "CHAOS_RUNNER_IMAGE=${LITMUS_RUNNER_IMAGE}" >/dev/null
  kubectl rollout status deployment/litmus -n litmus --timeout=180s

  echo "Installing Litmus namespace resources in ${CHAOS_NAMESPACE}..."
  kubectl create namespace "${CHAOS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  if ! kubectl get clusterrole "${CHAOS_CLUSTERROLE}" >/dev/null 2>&1; then
    cat <<EOF | kubectl apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${CHAOS_CLUSTERROLE}
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, pods/exec, events, configmaps, secrets, services]
    verbs: [get, list, watch, create, update, patch, delete, deletecollection]
  - apiGroups: ["apps"]
    resources: [deployments, replicasets, statefulsets, daemonsets]
    verbs: [get, list, watch, update, patch]
  - apiGroups: ["batch"]
    resources: [jobs]
    verbs: [get, list, watch, create, delete, deletecollection]
  - apiGroups: ["litmuschaos.io"]
    resources: [chaosengines, chaosexperiments, chaosresults]
    verbs: [get, list, watch, create, update, patch, delete]
EOF
  fi

  kubectl create serviceaccount "${CHAOS_SERVICEACCOUNT}" -n "${CHAOS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${CHAOS_CLUSTERROLE}-${CHAOS_NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${CHAOS_CLUSTERROLE}
subjects:
  - kind: ServiceAccount
    name: ${CHAOS_SERVICEACCOUNT}
    namespace: ${CHAOS_NAMESPACE}
EOF

  kubectl apply -f k8s/chaos/generic-experiments.yaml -n "${CHAOS_NAMESPACE}"
fi

echo "Prerequisites installed. Next: ./scripts/gke/07_build_and_push_images.sh"

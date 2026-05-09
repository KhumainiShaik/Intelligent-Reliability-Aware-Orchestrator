#!/usr/bin/env bash
set -euo pipefail

# 00_prereqs.sh — Verify local prerequisites for GKE setup.

for cmd in gcloud kubectl helm docker k6 gke-gcloud-auth-plugin; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: ${cmd} is required but not installed / not on PATH" >&2
    if [ "${cmd}" = "gke-gcloud-auth-plugin" ]; then
      echo "Install it with: gcloud components install gke-gcloud-auth-plugin" >&2
    fi
    exit 1
  fi
done

echo "gcloud: $(gcloud --version | head -n 1)"
echo "kubectl: $(kubectl version --client=true --short 2>/dev/null || true)"
echo "helm:   $(helm version --short 2>/dev/null || true)"
echo "docker: $(docker --version 2>/dev/null || true)"
echo "k6:     $(k6 version 2>/dev/null | head -n 1 || true)"
echo "gke auth plugin: $(gke-gcloud-auth-plugin --version 2>/dev/null || true)"

if command -v gsutil >/dev/null 2>&1; then
  echo "gsutil: $(gsutil version -l 2>/dev/null | head -n 1)"
else
  echo "WARNING: gsutil not found (GCS offload scripts won't work)" >&2
fi

echo "\nGCP identity:"
gcloud auth list

echo "\nActive config:"
gcloud config list --format='text(core.project,core.account,compute.region,compute.zone)'

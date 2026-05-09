#!/usr/bin/env bash
set -euo pipefail

# 07_build_and_push_images.sh — build and push controller + workload images to Artifact Registry.
#
# Usage:
#   PROJECT_ID="my-project" REGION="europe-west2" AR_REPO="oroll" \
#   IMAGE_TAG="20260331" RELEASE_TAG="v2.0.0" ./scripts/gke/07_build_and_push_images.sh
#
# Optional:
#   BUILD_ML_WORKLOAD=1   # also build/push ml-workload (MobileNetV2 ONNX)
#   ML_RELEASE_TAG=v1.0.0 # additional stable tag for the ML image

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west2}"
AR_REPO="${AR_REPO:-oroll}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d_%H%M%S)}"
RELEASE_TAG="${RELEASE_TAG:-v2.0.0}"
BUILD_ML_WORKLOAD="${BUILD_ML_WORKLOAD:-0}"
ML_RELEASE_TAG="${ML_RELEASE_TAG:-v1.0.0}"

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project" >&2
  exit 2
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

CTRL_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/orchestrated-rollout-controller"
WORK_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/orchestrated-rollout-workload"
ML_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/orchestrated-rollout-ml-workload"

echo "Building and pushing:"
echo "  Controller: ${CTRL_REPO}:${IMAGE_TAG}"
echo "  Workload:   ${WORK_REPO}:${IMAGE_TAG} and ${WORK_REPO}:${RELEASE_TAG}"
if [ "${BUILD_ML_WORKLOAD}" = "1" ]; then
  echo "  ML workload: ${ML_REPO}:${IMAGE_TAG} and ${ML_REPO}:${ML_RELEASE_TAG}"
fi

# Controller
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t "${CTRL_REPO}:${IMAGE_TAG}" -f controller/Dockerfile .
docker push "${CTRL_REPO}:${IMAGE_TAG}"

# Workload
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t "${WORK_REPO}:${IMAGE_TAG}" -f workload/Dockerfile .
docker tag "${WORK_REPO}:${IMAGE_TAG}" "${WORK_REPO}:${RELEASE_TAG}"
docker push "${WORK_REPO}:${IMAGE_TAG}"
docker push "${WORK_REPO}:${RELEASE_TAG}"

if [ "${BUILD_ML_WORKLOAD}" = "1" ]; then
  if [ ! -f "ml-workload/model/mobilenetv2.onnx" ]; then
    echo "ERROR: ml-workload/model/mobilenetv2.onnx is missing." >&2
    echo "Run: python3 ml-workload/export_model.py" >&2
    exit 2
  fi

  DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t "${ML_REPO}:${IMAGE_TAG}" -f ml-workload/Dockerfile .
  docker tag "${ML_REPO}:${IMAGE_TAG}" "${ML_REPO}:${ML_RELEASE_TAG}"
  docker push "${ML_REPO}:${IMAGE_TAG}"
  docker push "${ML_REPO}:${ML_RELEASE_TAG}"
fi

cat <<EOF

Export these for subsequent steps:
  export CTRL_IMAGE_REPO="${CTRL_REPO}"
  export CTRL_IMAGE_TAG="${IMAGE_TAG}"
  export WORK_IMAGE_REPO="${WORK_REPO}"
  export WORK_IMAGE_TAG="${IMAGE_TAG}"
  export RELEASE_IMAGE="${WORK_REPO}"
  export RELEASE_TAG="${RELEASE_TAG}"
  export ML_IMAGE_REPO="${ML_REPO}"
  export ML_IMAGE_TAG="${IMAGE_TAG}"
  export ML_RELEASE_TAG="${ML_RELEASE_TAG}"

Next: ./scripts/gke/08_deploy_system.sh
EOF

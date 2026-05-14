#!/usr/bin/env bash
set -euo pipefail

# update_rollout_config.sh — Update GitOps workload configuration with new version and rollout strategy
#
# Updates gitops/workloads/cpu-bound-fastapi/values.yaml with:
#   - workload.version
#   - image.tag
#   - orchestratedRollout strategy parameters
#
# Environment variables (all required):
#   WORKLOAD_VALUES_PATH       # Path to values.yaml
#   WORKLOAD_VERSION           # New workload version (e.g., v2.0.0)
#   IMAGE_TAG                  # New image tag
#   TRAFFIC_PROFILE            # Traffic profile (e.g., ramp)
#   OBJECTIVE                  # Optimization objective (e.g., reliability)
#   POLICY_VARIANT             # Policy variant (e.g., v12-contextual)
#   FAULT_CONTEXT              # Fault injection context (e.g., none)

WORKLOAD_VALUES_PATH="${WORKLOAD_VALUES_PATH:?WORKLOAD_VALUES_PATH not set}"
WORKLOAD_VERSION="${WORKLOAD_VERSION:?WORKLOAD_VERSION not set}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG not set}"
TRAFFIC_PROFILE="${TRAFFIC_PROFILE:?TRAFFIC_PROFILE not set}"
OBJECTIVE="${OBJECTIVE:?OBJECTIVE not set}"
POLICY_VARIANT="${POLICY_VARIANT:?POLICY_VARIANT not set}"
FAULT_CONTEXT="${FAULT_CONTEXT:?FAULT_CONTEXT not set}"

if [ ! -f "${WORKLOAD_VALUES_PATH}" ]; then
  echo "ERROR: ${WORKLOAD_VALUES_PATH} not found" >&2
  exit 1
fi

echo "Updating ${WORKLOAD_VALUES_PATH}..."
echo "  Version: ${WORKLOAD_VERSION}"
echo "  Image Tag: ${IMAGE_TAG}"
echo "  Traffic Profile: ${TRAFFIC_PROFILE}"
echo "  Objective: ${OBJECTIVE}"
echo "  Policy Variant: ${POLICY_VARIANT}"
echo "  Fault Context: ${FAULT_CONTEXT}"

# Use Python to safely update YAML
python3 << PYEOF
import yaml
import sys

values_path = "${WORKLOAD_VALUES_PATH}"

try:
    with open(values_path, 'r') as f:
        values = yaml.safe_load(f) or {}
except Exception as e:
    print(f"ERROR reading {values_path}: {e}", file=sys.stderr)
    sys.exit(1)

# Ensure nested structures exist
if 'workload' not in values:
    values['workload'] = {}
if 'image' not in values:
    values['image'] = {}
if 'orchestratedRollout' not in values:
    values['orchestratedRollout'] = {}

# Update values
values['workload']['version'] = "${WORKLOAD_VERSION}"
values['image']['tag'] = "${IMAGE_TAG}"

# Update orchestratedRollout config
values['orchestratedRollout']['enabled'] = True
values['orchestratedRollout']['actionSet'] = ['rl']
values['orchestratedRollout']['trafficProfile'] = "${TRAFFIC_PROFILE}"
values['orchestratedRollout']['objective'] = "${OBJECTIVE}"
values['orchestratedRollout']['policyVariant'] = "${POLICY_VARIANT}"
values['orchestratedRollout']['faultContext'] = "${FAULT_CONTEXT}"

# Write back with preserved formatting
try:
    with open(values_path, 'w') as f:
        yaml.dump(values, f, default_flow_style=False, sort_keys=False)
    print(f"✓ Updated {values_path}")
except Exception as e:
    print(f"ERROR writing {values_path}: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

# Export for downstream jobs
cat > update-config.env << ENV_EOF
WORKLOAD_VALUES_PATH=${WORKLOAD_VALUES_PATH}
WORKLOAD_VERSION=${WORKLOAD_VERSION}
IMAGE_TAG=${IMAGE_TAG}
TRAFFIC_PROFILE=${TRAFFIC_PROFILE}
POLICY_VARIANT=${POLICY_VARIANT}
ENV_EOF

echo "✓ Configuration updated successfully"

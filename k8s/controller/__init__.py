# OrchestratedRollout Controller — Python implementation
# Kubernetes operator for deploy-time orchestration using offline-trained RL.
"""
Modules:
    config      — Centralised configuration (env vars → frozen dataclass)
    snapshot    — Decision Snapshot collector (Prometheus + K8s API)
    stress      — StressScore computation
    policy      — Q-table policy engine (RL inference)
    guardrails  — Safety constraint enforcement
    materialiser— Argo Rollouts resource builder
    episode     — Episode record logging
    reconciler  — kopf-based reconciliation handler
"""

__version__ = "0.1.0"

__all__ = [
    "config",
    "episode",
    "guardrails",
    "materialiser",
    "policy",
    "reconciler",
    "snapshot",
    "stress",
]

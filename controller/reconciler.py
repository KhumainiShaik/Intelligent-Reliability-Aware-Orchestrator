"""
OrchestratedRollout Reconciler — kopf-based Kubernetes operator.

Watches OrchestratedRollout custom resources and executes the
Snapshot → StressScore → Policy → Guardrails → Materialise → Log
pipeline exactly as specified in the architecture document.

All mutable dependencies are stored in a ``_Registry`` dataclass
initialised once at startup. Individual modules receive only what
they need — no module-level mutable global singletons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import kopf
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from controller.config import ControllerConfig, load_config
from controller.episode import EpisodeLogger, EpisodeRecord
from controller.guardrails import Guardrails
from controller.materialiser import Materialiser
from controller.policy import (
    ACTION_CANARY,
    ACTION_DELAY,
    ACTION_PRE_SCALE,
    ACTION_ROLLING,
    Engine,
)
from controller.snapshot import Collector, DecisionSnapshot, degraded_snapshot
from controller.stress import Calculator

logger = logging.getLogger(__name__)

# CRD constants

CRD_GROUP = "rollout.orchestrated.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "orchestratedrollouts"

# Phases
PHASE_PENDING = "Pending"
PHASE_CHOOSING = "ChoosingStrategy"
PHASE_EXECUTING = "Executing"
PHASE_COMPLETED = "Completed"
PHASE_ABORTED = "Aborted"
PHASE_FAILED = "Failed"

TERMINAL_PHASES = {PHASE_COMPLETED, PHASE_FAILED, PHASE_ABORTED}


# Dependency registry (initialised once at startup)


@dataclass
class _Registry:
    """Holds references to all sub-modules, initialised once at startup."""

    cfg: ControllerConfig
    snap_collector: Collector
    guardrails: Guardrails
    materialiser: Materialiser
    episode_logger: EpisodeLogger
    custom_api: k8s_client.CustomObjectsApi
    policy_engine: Engine | None = None


_registry: _Registry | None = None


def _init_registry(cfg: ControllerConfig) -> _Registry:
    """Build the dependency registry from a loaded config."""
    collector = Collector.from_config(cfg)
    materialiser = Materialiser.from_config(cfg)
    episode_logger = EpisodeLogger(cfg.episode_log_path)
    guardrails = Guardrails()
    custom_api = k8s_client.CustomObjectsApi()

    policy_engine: Engine | None = None
    try:
        policy_engine = Engine(cfg.policy_path)
    except Exception as exc:
        logger.warning("Policy engine not available — using rule-based fallback: %s", exc)

    return _Registry(
        cfg=cfg,
        snap_collector=collector,
        guardrails=guardrails,
        materialiser=materialiser,
        episode_logger=episode_logger,
        custom_api=custom_api,
        policy_engine=policy_engine,
    )


# Rule-based fallback policy


def _rule_based(snap: DecisionSnapshot, stress_score: float) -> str:
    """Simple rule-based policy (no RL)."""
    if stress_score > 0.8:
        return ACTION_DELAY
    if stress_score > 0.6:
        return ACTION_CANARY
    if stress_score > 0.4 and snap.hpa_desired_replicas > snap.hpa_current_replicas:
        return ACTION_PRE_SCALE
    return ACTION_ROLLING


# Status patch helper


def _patch_status(namespace: str, name: str, status_body: dict) -> None:
    """Patch the status subresource of an OrchestratedRollout CR."""
    if _registry is None:
        raise RuntimeError("Controller not initialised; call on_startup first")
    _registry.custom_api.patch_namespaced_custom_object_status(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural=CRD_PLURAL,
        name=name,
        body={"status": status_body},
    )


# Kopf startup


@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Initialise sub-modules and configure kopf settings."""
    global _registry

    # Load in-cluster or local kubeconfig
    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        logger.info("Loaded local kubeconfig")

    cfg = load_config()
    _registry = _init_registry(cfg)

    # Kopf tuning
    settings.posting.level = logging.WARNING
    settings.watching.server_timeout = 270
    settings.watching.client_timeout = 300

    logger.info("OrchestratedRollout controller started")


# Reconciliation handler


@kopf.on.create(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
@kopf.on.update(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
@kopf.on.resume(CRD_GROUP, CRD_VERSION, CRD_PLURAL)  # type: ignore[arg-type]
def reconcile(
    spec: dict,
    status: dict,
    meta: dict,
    namespace: str,
    name: str,
    uid: str,
    body: dict,
    **_: Any,
) -> dict:
    """
    Main reconciliation loop for an OrchestratedRollout CR.

    Pipeline:
      1. Skip terminal phases
      2. Update phase → ChoosingStrategy
      3. Collect Decision Snapshot
      4. Compute StressScore
      5. Policy inference (RL or rule-based)
      6. Apply guardrails
      7. Update CR status
      8. Materialise Argo Rollout resources
      9. Log episode record
    """
    if _registry is None:
        raise kopf.TemporaryError("Controller not initialised yet", delay=5)

    reg = _registry
    # Per-CR stress calculator to avoid shared EWMA state across CRs
    stress_calc = Calculator.from_config(reg.cfg)

    current_phase = status.get("phase", "")

    # 1. Skip terminal phases
    if current_phase in TERMINAL_PHASES:
        logger.info(
            "[%s/%s] Skipping — already in terminal phase %s", namespace, name, current_phase
        )
        return {"phase": current_phase}

    # 2. Transition to ChoosingStrategy
    if current_phase in ("", PHASE_PENDING):
        _patch_status(namespace, name, {"phase": PHASE_CHOOSING})
        logger.info("[%s/%s] Phase → %s", namespace, name, PHASE_CHOOSING)

    # 3. Collect Decision Snapshot
    target_name = spec.get("targetRef", {}).get("name", "workload")
    try:
        snap = reg.snap_collector.collect(namespace, target_name)
    except Exception as exc:
        logger.warning(
            "[%s/%s] Snapshot collection failed — degraded mode: %s", namespace, name, exc
        )
        snap = degraded_snapshot()

    logger.info(
        "[%s/%s] Snapshot: rps=%.1f  p95=%.1fms  errRate=%.4f  pending=%d",
        namespace,
        name,
        snap.rps,
        snap.p95_latency_ms,
        snap.error_rate,
        snap.pending_pods,
    )

    # 4. Compute StressScore
    stress_score = stress_calc.compute(snap)
    logger.info("[%s/%s] StressScore=%.3f", namespace, name, stress_score)

    # 5. Policy inference
    allowed_actions = spec.get("actionSet", [])
    policy_version = "rule-based"
    chosen_action = _rule_based(snap, stress_score)

    if reg.policy_engine is not None:
        try:
            chosen_action, policy_version = reg.policy_engine.select_action(
                snap, stress_score, allowed_actions or None
            )
        except Exception as exc:
            logger.warning(
                "[%s/%s] Policy inference failed — rule-based fallback: %s", namespace, name, exc
            )
            chosen_action = _rule_based(snap, stress_score)
            policy_version = "rule-based-fallback"

    logger.info("[%s/%s] Action=%s (policy=%s)", namespace, name, chosen_action, policy_version)

    # 6. Guardrails
    guardrail_cfg = spec.get("guardrailConfig")
    final_action, overridden, reason = reg.guardrails.apply(
        chosen_action, snap, stress_score, guardrail_cfg
    )
    if overridden:
        logger.info(
            "[%s/%s] Guardrail override: %s → %s (%s)",
            namespace,
            name,
            chosen_action,
            final_action,
            reason,
        )

    # 7. Update CR status → Executing
    now_iso = datetime.now(UTC).isoformat()
    new_status = {
        "phase": PHASE_EXECUTING,
        "chosenStrategy": final_action,
        "policyVersion": policy_version,
        "stressScore": round(stress_score, 4),
        "decisionTimestamp": now_iso,
        "startTimestamp": now_iso,
        "message": f"Selected {final_action} (stress={stress_score:.2f})",
    }
    try:
        _patch_status(namespace, name, new_status)
    except Exception as exc:
        logger.error("[%s/%s] Failed to patch status: %s", namespace, name, exc)

    # 8. Materialise Argo Rollout
    try:
        reg.materialiser.apply(body, final_action)
    except Exception as exc:
        logger.error("[%s/%s] Materialisation failed: %s", namespace, name, exc)
        _patch_status(
            namespace,
            name,
            {
                "phase": PHASE_FAILED,
                "message": f"materialisation failed: {exc}",
            },
        )
        raise kopf.PermanentError(f"materialisation failed: {exc}") from exc

    # 9. Episode record
    record = EpisodeRecord(
        run_id=uid,
        timestamp=datetime.now(UTC).strftime("%Y%m%d_%H%M%S"),
        namespace=namespace,
        name=name,
        policy_version=policy_version,
        decision_snapshot=snap.to_dict(),
        stress_score=stress_score,
        chosen_action=final_action,
        original_action=chosen_action,
        guardrail_override=overridden,
        override_reason=reason,
    )
    try:
        reg.episode_logger.write(record)
    except Exception as exc:
        logger.warning("[%s/%s] Failed to write episode record: %s", namespace, name, exc)

    logger.info(
        "[%s/%s] Reconciliation complete — strategy=%s  stress=%.3f",
        namespace,
        name,
        final_action,
        stress_score,
    )

    return {
        "phase": PHASE_EXECUTING,
        "chosenStrategy": final_action,
        "stressScore": round(stress_score, 4),
    }

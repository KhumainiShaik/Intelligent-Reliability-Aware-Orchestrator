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
from typing import TYPE_CHECKING, Any

import kopf
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from controller.config import ControllerConfig, load_config
from controller.episode import EpisodeLogger, EpisodeRecord
from controller.guardrails import Guardrails, get_max_extra_replicas
from controller.materialiser import ROLLOUT_GROUP, ROLLOUT_PLURAL, Materialiser
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

if TYPE_CHECKING:
    from collections.abc import Mapping

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
ACTION_RL = "rl"
ACTION_RULE_BASED = "rule-based"
POLICY_VARIANT_V12 = "v12-contextual"
FIXED_BASELINE_ACTIONS = {
    ACTION_DELAY,
    ACTION_PRE_SCALE,
    ACTION_CANARY,
    ACTION_ROLLING,
}


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
    if cfg.policy_mode not in ("rl", "rule-based"):
        logger.warning(
            "Unknown POLICY_MODE=%r — expected 'rl' or 'rule-based'; defaulting to rule-based",
            cfg.policy_mode,
        )
    if cfg.policy_mode == "rl":
        try:
            policy_engine = Engine(cfg.policy_path)
        except Exception as exc:
            logger.warning("Policy engine not available — using rule-based fallback: %s", exc)
    else:
        logger.info("Policy engine disabled (POLICY_MODE=%s)", cfg.policy_mode)

    return _Registry(
        cfg=cfg,
        snap_collector=collector,
        guardrails=guardrails,
        materialiser=materialiser,
        episode_logger=episode_logger,
        custom_api=custom_api,
        policy_engine=policy_engine,
    )


# Rule-based fallback/baseline policy

def _rule_based_decision(
    snap: DecisionSnapshot,
    stress_score: float,
    rollout_hints: dict | None = None,
    guardrail_cfg: dict | None = None,
) -> tuple[str, int | None, str]:
    """Deterministic rule baseline using the same snapshot/hint inputs as RL.

    The policy is deliberately simple and transparent. It exists to answer the
    evaluation question: "does the learned/adaptive path add value beyond a
    hand-written threshold selector?"
    """
    hints = rollout_hints if isinstance(rollout_hints, dict) else {}
    profile = str(hints.get("trafficProfile", "")).strip().lower()
    objective = str(hints.get("objective", "reliability")).strip().lower()
    if objective in {"", "unknown"}:
        objective = "reliability"

    max_extra = get_max_extra_replicas(guardrail_cfg)
    live_pressure = (
        snap.pending_pods > 0
        or snap.hpa_desired_replicas > snap.hpa_current_replicas
        or snap.node_cpu_util >= 0.75
        or stress_score >= 0.40
    )

    if stress_score > 0.90 or snap.node_cpu_util > 0.90 or snap.node_mem_util > 0.90:
        return ACTION_DELAY, None, "extreme stress"
    if live_pressure:
        return ACTION_PRE_SCALE, max_extra, "live autoscaling/resource pressure"
    if objective in {"reliability", "safety", "safe"}:
        return ACTION_PRE_SCALE, max_extra, "reliability objective"
    if profile == "spike":
        return ACTION_PRE_SCALE, max_extra, "spike traffic profile"
    if profile == "ramp":
        return ACTION_PRE_SCALE, 3, "ramp traffic profile"
    if snap.error_rate > 0.01:
        return ACTION_CANARY, None, "elevated error rate"
    return ACTION_ROLLING, None, "healthy steady/default"


def _rule_based(snap: DecisionSnapshot, stress_score: float) -> str:
    """Compatibility wrapper returning only the rule action."""
    action, _extra, _reason = _rule_based_decision(snap, stress_score)
    return action


def _rollout_context(rollout_hints: dict | None) -> tuple[str, str, str]:
    """Return normalised traffic, fault, and objective rollout context."""
    hints = rollout_hints if isinstance(rollout_hints, dict) else {}
    profile = str(hints.get("trafficProfile", "")).strip().lower() or "unknown"
    fault = str(hints.get("faultContext", hints.get("fault", ""))).strip().lower() or "unknown"
    objective = str(hints.get("objective", "reliability")).strip().lower()
    if objective in {"", "unknown"}:
        objective = "reliability"
    return profile, fault, objective


def _is_v12_contextual(rollout_hints: dict | None) -> bool:
    """True when the rollout explicitly opts into the v12 candidate policy."""
    hints = rollout_hints if isinstance(rollout_hints, dict) else {}
    variant = str(hints.get("policyVariant", "")).strip().lower()
    return variant in {POLICY_VARIANT_V12, "v12", "contextual-rl"}


def _v12_contextual_decision(
    snap: DecisionSnapshot,
    stress_score: float,
    rollout_hints: dict | None = None,
    guardrail_cfg: dict | None = None,
) -> tuple[str, int | None, str]:
    """Candidate contextual RL policy for action intensity experiments.

    v12 is deliberately opt-in and leaves the validated v11 path untouched. It
    implements the minimum useful parameterised action space for holdout tests:
    rolling, pre-scale +3/+5/+7, and delay for extreme safety cases.
    """
    profile, fault, objective = _rollout_context(rollout_hints)
    max_extra = max(7, get_max_extra_replicas(guardrail_cfg)) if _is_v12_contextual(rollout_hints) else get_max_extra_replicas(guardrail_cfg)
    hpa_gap = snap.hpa_desired_replicas - snap.hpa_current_replicas
    live_pressure = (
        snap.pending_pods > 0
        or hpa_gap > 0
        or snap.node_cpu_util >= 0.75
        or snap.error_rate > 0.005
        or stress_score >= 0.20
    )
    extreme_pressure = (
        snap.degraded
        or stress_score > 0.90
        or snap.node_cpu_util > 0.90
        or snap.node_mem_util > 0.90
        or snap.pending_pods > 5
    )
    fault_active = fault not in {"", "none", "unknown", "no-fault"}

    if extreme_pressure:
        return ACTION_DELAY, None, "v12 extreme pressure -> delay-30"

    if (
        profile == "ramp"
        and not fault_active
        and objective in {"reliability", "safety", "safe"}
        and not live_pressure
    ):
        return ACTION_ROLLING, None, "v12 low-risk ramp/none -> rolling-cautious"

    if profile == "ramp" and not fault_active:
        return ACTION_PRE_SCALE, min(max_extra, 3), "v12 ramp/none pressure -> pre-scale-3"

    if profile == "spike" and fault_active and (hpa_gap > 0 or snap.node_cpu_util >= 0.70 or stress_score >= 0.10):
        return ACTION_PRE_SCALE, min(max_extra, 7), "v12 high-risk spike/fault -> pre-scale-7"

    if profile == "spike":
        return ACTION_PRE_SCALE, min(max_extra, 5), "v12 spike -> pre-scale-5"

    if fault_active or live_pressure:
        return ACTION_PRE_SCALE, min(max_extra, 5), "v12 fault/live pressure -> pre-scale-5"

    return ACTION_PRE_SCALE, min(max_extra, 3), "v12 low pressure -> pre-scale-3"


def _is_action_allowed(action: str, allowed_actions: Any) -> bool:
    """Return True when *action* is permitted by an optional actionSet."""
    if not isinstance(allowed_actions, list) or not allowed_actions:
        return True
    action_set = {str(item) for item in allowed_actions if item}
    if action_set == {ACTION_RL}:
        return True
    action_set.discard(ACTION_RL)
    action_set.discard(ACTION_RULE_BASED)
    if not action_set:
        return True
    return action in action_set


def _single_action_set_value(allowed_actions: Any) -> str | None:
    """Return the only actionSet value when the CR declares exactly one."""
    if not isinstance(allowed_actions, list) or len(allowed_actions) != 1:
        return None
    value = str(allowed_actions[0]).strip()
    return value or None


def _is_fixed_baseline_action_set(allowed_actions: Any) -> bool:
    """True for fixed strategy baselines that must materialise unchanged."""
    return _single_action_set_value(allowed_actions) in FIXED_BASELINE_ACTIONS


def _is_rule_based_action_set(allowed_actions: Any) -> bool:
    """True when actionSet requests the deterministic adaptive selector."""
    return _single_action_set_value(allowed_actions) == ACTION_RULE_BASED


def _policy_allowed_actions(allowed_actions: Any) -> list[str] | None:
    """Return concrete policy actions after removing selector-mode labels."""
    if not isinstance(allowed_actions, list):
        return None
    actions = [
        str(item).strip()
        for item in allowed_actions
        if str(item).strip() in FIXED_BASELINE_ACTIONS
    ]
    return actions or None


def _apply_rollout_hints(
    action: str,
    snap: DecisionSnapshot,
    stress_score: float,
    rollout_hints: dict | None,
    allowed_actions: Any = None,
) -> tuple[str, bool, str]:
    """Apply optional deployment-context hints after policy selection.

    Forecasting is intentionally optional. A deployment request may still carry
    explicit context known by the release pipeline, such as whether traffic is
    steady, ramping, or spiking. The optional objective controls whether the
    hint should prioritise reliability or latency/cost. These hints are
    advisory and never override a fixed baseline actionSet.
    """
    if not isinstance(rollout_hints, dict):
        return action, False, ""

    profile = str(rollout_hints.get("trafficProfile", "")).strip().lower()
    if not profile:
        return action, False, ""

    objective = str(rollout_hints.get("objective", "reliability")).strip().lower()
    if objective in {"", "unknown"}:
        objective = "reliability"

    candidate = ""
    if objective in {"reliability", "safety", "safe"}:
        if profile in {"steady", "ramp", "spike"}:
            candidate = ACTION_PRE_SCALE
    elif profile == "spike":
        candidate = ACTION_PRE_SCALE
    elif profile == "ramp":
        candidate = ACTION_ROLLING
    elif profile == "steady":
        candidate = ACTION_CANARY if stress_score < 0.25 and snap.error_rate <= 0.01 else ACTION_ROLLING

    if not candidate or candidate == action or not _is_action_allowed(candidate, allowed_actions):
        return action, False, ""

    return candidate, True, (
        f"rollout hint trafficProfile={profile}, objective={objective} -> {candidate}"
    )


def _adaptive_pre_scale_extra_replicas(
    snap: DecisionSnapshot,
    stress_score: float,
    rollout_hints: dict | None,
    guardrail_cfg: dict | None,
) -> tuple[int | None, str]:
    """Return adaptive pre-scale extra replicas for non-baseline rollouts.

    Fixed Pre-Scale baselines remain at the materialiser default of +3. The
    adaptive path may request more headroom when the release pipeline declares a
    reliability objective or the live snapshot already shows pressure.
    """
    if not isinstance(rollout_hints, dict):
        return None, ""

    profile = str(rollout_hints.get("trafficProfile", "")).strip().lower()
    objective = str(rollout_hints.get("objective", "reliability")).strip().lower()
    if objective in {"", "unknown"}:
        objective = "reliability"

    if objective not in {"reliability", "safety", "safe", "performance"}:
        return None, ""

    max_extra = get_max_extra_replicas(guardrail_cfg)
    extra = 3
    if profile in {"ramp", "spike"}:
        extra = max_extra
    elif profile == "steady":
        extra = min(max_extra, 4)

    live_pressure = (
        snap.error_rate > 0.005
        or snap.pending_pods > 0
        or snap.hpa_desired_replicas > snap.hpa_current_replicas
        or stress_score >= 0.10
    )
    if live_pressure:
        extra = max(extra, max_extra)

    extra = max(3, min(max_extra, extra))
    if extra <= 3:
        return None, ""
    return extra, f"adaptive headroom profile={profile or 'unknown'}, objective={objective}, extra={extra}"


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
    rule_extra_replicas: int | None = None
    v12_extra_replicas: int | None = None
    chosen_action = _rule_based(snap, stress_score)
    hint_overridden = False
    hint_reason = ""

    # Baselines: if the CR constrains the action set to a single action, treat
    # it as a baseline for analysis/plot grouping. "rule-based" is special: it
    # selects an action deterministically instead of materialising an action
    # named "rule-based".
    is_rule_based_baseline = _is_rule_based_action_set(allowed_actions)
    is_fixed_baseline = _is_fixed_baseline_action_set(allowed_actions)
    is_v12_candidate = _is_v12_contextual(spec.get("rolloutHints"))
    if is_rule_based_baseline:
        chosen_action, rule_extra_replicas, rule_reason = _rule_based_decision(
            snap,
            stress_score,
            spec.get("rolloutHints"),
            spec.get("guardrailConfig"),
        )
        policy_version = "baseline-rule-based"
        logger.info(
            "[%s/%s] Rule-based baseline decision: %s (%s)",
            namespace,
            name,
            chosen_action,
            rule_reason,
        )
    elif is_fixed_baseline:
        chosen_action = str(allowed_actions[0])
        policy_version = f"baseline-{chosen_action}"
    elif is_v12_candidate:
        chosen_action, v12_extra_replicas, v12_reason = _v12_contextual_decision(
            snap,
            stress_score,
            spec.get("rolloutHints"),
            spec.get("guardrailConfig"),
        )
        policy_version = POLICY_VARIANT_V12
        logger.info(
            "[%s/%s] v12 contextual decision: %s (%s)",
            namespace,
            name,
            chosen_action,
            v12_reason,
        )
    elif reg.policy_engine is not None:
        try:
            chosen_action, policy_version = reg.policy_engine.select_action(
                snap, stress_score, _policy_allowed_actions(allowed_actions)
            )
        except Exception as exc:
            logger.warning(
                "[%s/%s] Policy inference failed — rule-based fallback: %s", namespace, name, exc
            )
            chosen_action = _rule_based(snap, stress_score)
            policy_version = "rule-based-fallback"

    if not is_fixed_baseline and not is_rule_based_baseline and not is_v12_candidate:
        hinted_action, hint_overridden, hint_reason = _apply_rollout_hints(
            chosen_action,
            snap,
            stress_score,
            spec.get("rolloutHints"),
            allowed_actions,
        )
        if hint_overridden:
            logger.info(
                "[%s/%s] Rollout hint override: %s → %s (%s)",
                namespace,
                name,
                chosen_action,
                hinted_action,
                hint_reason,
            )
            chosen_action = hinted_action
            policy_version = f"{policy_version}+hints"

    logger.info("[%s/%s] Action=%s (policy=%s)", namespace, name, chosen_action, policy_version)

    # 6. Guardrails
    guardrail_cfg = spec.get("guardrailConfig")
    if is_fixed_baseline:
        final_action, overridden, reason = chosen_action, False, ""
        logger.info(
            "[%s/%s] Fixed baseline %s bypasses hints and guardrail strategy overrides",
            namespace,
            name,
            final_action,
        )
    else:
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

    adaptive_extra_replicas: int | None = None
    adaptive_capacity_reason = ""
    if final_action == ACTION_PRE_SCALE and is_rule_based_baseline:
        adaptive_extra_replicas = rule_extra_replicas
        if adaptive_extra_replicas is not None:
            logger.info(
                "[%s/%s] Rule-based pre-scale capacity: +%d replicas",
                namespace,
                name,
                adaptive_extra_replicas,
            )
    elif final_action == ACTION_PRE_SCALE and is_v12_candidate:
        adaptive_extra_replicas = v12_extra_replicas
        if adaptive_extra_replicas is not None:
            logger.info(
                "[%s/%s] v12 pre-scale capacity: +%d replicas",
                namespace,
                name,
                adaptive_extra_replicas,
            )
    elif final_action == ACTION_PRE_SCALE and not is_fixed_baseline:
        adaptive_extra_replicas, adaptive_capacity_reason = _adaptive_pre_scale_extra_replicas(
            snap,
            stress_score,
            spec.get("rolloutHints"),
            guardrail_cfg,
        )
        if adaptive_extra_replicas is not None:
            logger.info(
                "[%s/%s] Adaptive pre-scale capacity: +%d replicas (%s)",
                namespace,
                name,
                adaptive_extra_replicas,
                adaptive_capacity_reason,
            )
            policy_version = f"{policy_version}+adaptive-capacity"

    # 7. Update CR status → Executing
    now_iso = datetime.now(UTC).isoformat()
    status_message = f"Selected {final_action} (stress={stress_score:.2f})"
    if adaptive_extra_replicas is not None:
        status_message += f"; adaptive pre-scale extra={adaptive_extra_replicas}"
    new_status = {
        "phase": PHASE_EXECUTING,
        "chosenStrategy": final_action,
        "policyVersion": policy_version,
        "stressScore": round(stress_score, 4),
        "decisionTimestamp": now_iso,
        "startTimestamp": now_iso,
        "message": status_message,
    }
    if final_action == ACTION_PRE_SCALE:
        new_status["preScaleExtraReplicas"] = adaptive_extra_replicas or 3
    try:
        _patch_status(namespace, name, new_status)
    except Exception as exc:
        logger.error("[%s/%s] Failed to patch status: %s", namespace, name, exc)

    # 8. Materialise Argo Rollout
    try:
        materialiser_extra_replicas = adaptive_extra_replicas
        if is_fixed_baseline and final_action == ACTION_PRE_SCALE:
            materialiser_extra_replicas = 3
        reg.materialiser.apply(
            body,
            final_action,
            pre_scale_extra_replicas=materialiser_extra_replicas,
        )
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


@kopf.on.event(
    ROLLOUT_GROUP,
    "v1alpha1",
    ROLLOUT_PLURAL,
    labels={"orchestrated-rollout.io/name": kopf.PRESENT},
)
def on_rollout_event(
    *,
    type: str,
    event: kopf.RawEvent,
    annotations: Mapping[str, str],
    labels: Mapping[str, str],
    body: kopf.Body,
    meta: kopf.Meta,
    spec: kopf.Spec,
    status: kopf.Status,
    resource: kopf.Resource,
    uid: str | None,
    name: str | None,
    namespace: str | None,
    patch: kopf.Patch,
    logger: logging.Logger | logging.LoggerAdapter[Any],
    memo: Any,
    param: Any = None,
    **_: Any,
) -> None:
    """Close the loop: mark OrchestratedRollout terminal when Argo Rollout is terminal.

    The experiment harness waits for `.status.phase` to reach a terminal phase.
    Argo Rollouts reports terminality via `.status.phase` (e.g., Healthy/Degraded).
    """
    if _registry is None:
        return

    if type == "DELETED":
        return

    metadata = body.get("metadata") or {}
    labels = metadata.get("labels") or {}

    namespace = metadata.get("namespace")
    oroll_name = labels.get("orchestrated-rollout.io/name")
    rollout_name = metadata.get("name")
    if not namespace or not oroll_name:
        return

    rollout_phase = (body.get("status") or {}).get("phase")
    if rollout_phase == "Healthy":
        target_phase = PHASE_COMPLETED
    elif rollout_phase == "Degraded":
        target_phase = PHASE_FAILED
    else:
        return

    try:
        oroll = _registry.custom_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural=CRD_PLURAL,
            name=oroll_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return
        raise

    current_phase = (oroll.get("status") or {}).get("phase")
    if current_phase in TERMINAL_PHASES:
        return
    if current_phase != PHASE_EXECUTING:
        return

    logger.info(
        "[%s/%s] Rollout %s became %s; phase → %s",
        namespace,
        oroll_name,
        rollout_name,
        rollout_phase,
        target_phase,
    )
    _patch_status(
        namespace,
        oroll_name,
        {
            "phase": target_phase,
            "message": f"Argo Rollout {rollout_name} became {rollout_phase}",
        },
    )

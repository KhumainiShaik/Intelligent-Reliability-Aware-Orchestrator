"""
Guardrails — safety constraints applied after policy inference.

Six deterministic rules ensure the chosen action stays within safe
operating bounds regardless of what the RL policy recommends.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from controller.policy import (
    ACTION_CANARY,
    ACTION_DELAY,
    ACTION_PRE_SCALE,
    ACTION_ROLLING,
)

if TYPE_CHECKING:
    from controller.snapshot import DecisionSnapshot

logger = logging.getLogger(__name__)

DEFAULT_MAX_DELAY_SECONDS = 120
DEFAULT_MAX_EXTRA_REPLICAS = 5
DEFAULT_MAX_ROLLOUT_TIME_SECONDS = 600


# Helper: extract guardrail config values from CR spec


def _cfg_int(config: dict | None, key: str, default: int) -> int:
    """Safely extract an integer from the guardrailConfig dict."""
    if config and key in config:
        try:
            v = int(config[key])
            return v if v > 0 else default
        except (TypeError, ValueError):
            pass
    return default


def get_max_delay(config: dict | None) -> int:
    return _cfg_int(config, "maxDelaySeconds", DEFAULT_MAX_DELAY_SECONDS)


def get_max_extra_replicas(config: dict | None) -> int:
    return _cfg_int(config, "maxExtraReplicas", DEFAULT_MAX_EXTRA_REPLICAS)


def get_max_rollout_time(config: dict | None) -> int:
    return _cfg_int(config, "maxRolloutTimeSeconds", DEFAULT_MAX_ROLLOUT_TIME_SECONDS)


class Guardrails:
    """Apply six safety rules to override policy-chosen actions when needed."""

    def apply(
        self,
        action: str,
        snap: DecisionSnapshot,
        stress_score: float,
        config: dict | None = None,
    ) -> tuple[str, bool, str]:
        """
        Check *action* against safety constraints.

        Returns *(final_action, overridden, reason)*.
        """
        # Rule 1: degraded snapshot → force conservative canary
        if snap.degraded:
            if action != ACTION_CANARY:
                return ACTION_CANARY, True, "degraded snapshot — forcing conservative canary"
            return action, False, ""

        # Rule 2: severe node pressure → force delay
        if (snap.node_cpu_util > 0.9 or snap.node_mem_util > 0.9) and action in (
            ACTION_ROLLING,
            ACTION_PRE_SCALE,
        ):
            return ACTION_DELAY, True, "severe node pressure — forcing delay"

        # Rule 3: high pending pods → avoid rolling
        if snap.pending_pods > 5 and action == ACTION_ROLLING:
            return ACTION_DELAY, True, "high pending pods — forcing delay instead of rolling"

        # Rule 4: elevated error rate → prefer canary over rolling
        if snap.error_rate > 0.05 and action == ACTION_ROLLING:
            return ACTION_CANARY, True, "elevated error rate — forcing canary instead of rolling"

        # Rule 5: extreme stress score → conservative override
        if stress_score > 0.9 and action not in (ACTION_DELAY, ACTION_CANARY):
            return ACTION_DELAY, True, "extreme stress score (>0.9) — forcing delay"

        # Rule 6: pre-scale — materialiser enforces actual limit, guardrails allow it
        if action == ACTION_PRE_SCALE:
            _max_extra = get_max_extra_replicas(config)
            # (informational only; materialiser caps replicas)

        return action, False, ""

"""
RL Policy Engine — Q-table lookup and feature discretisation.

Loads a policy_artifact.json exported by the KISim Q-learning trainer and
provides action selection via discretised state → Q-value lookup.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.snapshot import DecisionSnapshot

logger = logging.getLogger(__name__)

ACTION_DELAY = "delay"
ACTION_PRE_SCALE = "pre-scale"
ACTION_CANARY = "canary"
ACTION_ROLLING = "rolling"

ALL_ACTIONS = [ACTION_DELAY, ACTION_PRE_SCALE, ACTION_CANARY, ACTION_ROLLING]


@dataclass
class FeatureSpec:
    name: str
    min_val: float
    max_val: float
    num_bins: int


@dataclass
class PolicyArtifact:
    """Versioned RL policy artifact (Q-table + discretisation info)."""

    version: str
    features: list[FeatureSpec]
    bins: dict[str, list[float]]
    q_table: dict[str, list[float]]  # key: discretised state string → Q-values
    actions: list[str]  # ordered action list matching Q-value indices

    @classmethod
    def from_file(cls, path: str | Path) -> PolicyArtifact:
        """Load artifact from a JSON file."""
        data = json.loads(Path(path).read_text())
        features = [
            FeatureSpec(
                name=f["name"],
                min_val=f["min_val"],
                max_val=f["max_val"],
                num_bins=f["num_bins"],
            )
            for f in data["features"]
        ]
        return cls(
            version=data["version"],
            features=features,
            bins=data["bins"],
            q_table=data["q_table"],
            actions=data["actions"],
        )


class Engine:
    """Performs policy inference using a loaded Q-table artifact."""

    def __init__(self, policy_path: str) -> None:
        artifact_file = os.path.join(policy_path, "policy_artifact.json")
        self._artifact = PolicyArtifact.from_file(artifact_file)

        if not self._artifact.q_table:
            raise ValueError("Empty Q-table in artifact")

        logger.info(
            "Policy engine loaded — version=%s, states=%d, actions=%s",
            self._artifact.version,
            len(self._artifact.q_table),
            self._artifact.actions,
        )

    def select_action(
        self,
        snap: DecisionSnapshot,
        stress_score: float,
        allowed_actions: list[str] | None = None,
    ) -> tuple[str, str]:
        """
        Return *(chosen_action, policy_version)*.

        Raises ``ValueError`` if no valid action can be found.
        """
        state_key = self._discretise(snap, stress_score)

        q_values = self._artifact.q_table.get(state_key)
        if q_values is None:
            raise ValueError(f"State {state_key} not found in Q-table")

        # Build allowed set
        allowed = set(self._artifact.actions) if not allowed_actions else set(allowed_actions)

        # Pick highest Q-value among allowed actions
        best_idx = -1
        best_q = -math.inf
        for i, action_name in enumerate(self._artifact.actions):
            if action_name in allowed and i < len(q_values) and q_values[i] > best_q:
                best_q = q_values[i]
                best_idx = i

        if best_idx < 0:
            raise ValueError("No valid action found in Q-table for allowed set")

        return self._artifact.actions[best_idx], self._artifact.version

    @property
    def version(self) -> str:
        return self._artifact.version

    def _discretise(self, snap: DecisionSnapshot, stress_score: float) -> str:
        """Convert snapshot + stress into a discrete state key string."""
        feature_values: dict[str, float] = {
            "rps": snap.rps,
            "p95_latency": snap.p95_latency_ms,
            "error_rate": snap.error_rate,
            "pending_pods": float(snap.pending_pods),
            "cpu_util": snap.node_cpu_util,
            "mem_util": snap.node_mem_util,
            "hpa_gap": float(snap.hpa_desired_replicas - snap.hpa_current_replicas),
            "stress_score": stress_score,
            # stress_forecast is optional. Older artifacts may include it as a
            # feature, while the default no-forecast policy ignores it.
            "stress_forecast": snap.stress_forecast,
        }

        parts: list[str] = []
        for feat in self._artifact.features:
            val = feature_values.get(feat.name, 0.0)
            bins = self._artifact.bins.get(feat.name, [])
            parts.append(str(_digitise(val, bins)))

        return "_".join(parts)


def _digitise(val: float, bins: list[float]) -> int:
    """Return the bin index for *val* given sorted bin edges."""
    for i, edge in enumerate(bins):
        if val < edge:
            return i
    return len(bins)

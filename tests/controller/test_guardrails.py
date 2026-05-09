"""Unit tests for controller.guardrails."""

from __future__ import annotations

import pytest

from controller.guardrails import Guardrails, get_max_delay, get_max_extra_replicas
from controller.snapshot import DecisionSnapshot


class TestGuardrails:
    """Tests for the six guardrail safety rules."""

    @pytest.fixture()
    def guard(self) -> Guardrails:
        return Guardrails()

    def test_no_override_healthy(
        self,
        guard: Guardrails,
        healthy_snapshot: DecisionSnapshot,
    ) -> None:
        """Healthy cluster → no override for any valid action."""
        action, overridden, reason = guard.apply("rolling", healthy_snapshot, 0.2)
        assert action == "rolling"
        assert overridden is False
        assert reason == ""

    def test_rule1_degraded_forces_canary(
        self,
        guard: Guardrails,
        degraded_snapshot: DecisionSnapshot,
    ) -> None:
        """Degraded snapshot → force canary."""
        action, overridden, reason = guard.apply("rolling", degraded_snapshot, 0.5)
        assert action == "canary"
        assert overridden is True
        assert "degraded" in reason

    def test_rule1_degraded_canary_unchanged(
        self,
        guard: Guardrails,
        degraded_snapshot: DecisionSnapshot,
    ) -> None:
        """Degraded snapshot with canary already chosen → no override."""
        action, overridden, _ = guard.apply("canary", degraded_snapshot, 0.5)
        assert action == "canary"
        assert overridden is False

    def test_rule2_low_stress_delay_forces_prescale(self, guard: Guardrails) -> None:
        """Healthy low-stress snapshots should not choose delay."""
        snap = DecisionSnapshot(
            error_rate=0.0,
            pending_pods=0,
            node_cpu_util=0.2,
            node_mem_util=0.3,
        )
        action, overridden, reason = guard.apply("delay", snap, 0.1)
        assert action == "pre-scale"
        assert overridden is True
        assert "low-stress" in reason

    def test_rule2_low_stress_does_not_override_under_pressure(self, guard: Guardrails) -> None:
        """Delay remains available when there is a concrete pressure signal."""
        snap = DecisionSnapshot(
            error_rate=0.0,
            pending_pods=6,
            node_cpu_util=0.2,
            node_mem_util=0.3,
        )
        action, overridden, _reason = guard.apply("delay", snap, 0.1)
        assert action == "delay"
        assert overridden is False

    def test_rule2_severe_cpu_forces_delay(self, guard: Guardrails) -> None:
        """High CPU → rolling/pre-scale forced to delay."""
        snap = DecisionSnapshot(node_cpu_util=0.95, node_mem_util=0.4)
        action, overridden, _reason = guard.apply("rolling", snap, 0.5)
        assert action == "delay"
        assert overridden is True

    def test_rule2_severe_mem_forces_delay(self, guard: Guardrails) -> None:
        """High memory → rolling forced to delay."""
        snap = DecisionSnapshot(node_cpu_util=0.4, node_mem_util=0.95)
        action, overridden, _reason = guard.apply("pre-scale", snap, 0.5)
        assert action == "delay"
        assert overridden is True

    def test_rule3_pending_pods_block_rolling(self, guard: Guardrails) -> None:
        """High pending pods → rolling forced to delay."""
        snap = DecisionSnapshot(pending_pods=10)
        action, overridden, _reason = guard.apply("rolling", snap, 0.3)
        assert action == "delay"
        assert overridden is True

    def test_rule4_error_rate_canary_over_rolling(self, guard: Guardrails) -> None:
        """Elevated error rate → rolling forced to canary."""
        snap = DecisionSnapshot(error_rate=0.08)
        action, overridden, _reason = guard.apply("rolling", snap, 0.3)
        assert action == "canary"
        assert overridden is True

    def test_rule5_extreme_stress_forces_delay(self, guard: Guardrails) -> None:
        """Stress > 0.9 → pre-scale/rolling forced to delay."""
        snap = DecisionSnapshot()
        action, overridden, _reason = guard.apply("pre-scale", snap, 0.95)
        assert action == "delay"
        assert overridden is True

    def test_rule5_delay_allowed_at_extreme_stress(self, guard: Guardrails) -> None:
        """Stress > 0.9 but action is already delay → no override."""
        snap = DecisionSnapshot()
        action, overridden, _ = guard.apply("delay", snap, 0.95)
        assert action == "delay"
        assert overridden is False


class TestGuardrailHelpers:
    """Tests for guardrail config extraction functions."""

    def test_get_max_delay_default(self) -> None:
        assert get_max_delay(None) == 120

    def test_get_max_delay_custom(self) -> None:
        assert get_max_delay({"maxDelaySeconds": 300}) == 300

    def test_get_max_delay_invalid(self) -> None:
        assert get_max_delay({"maxDelaySeconds": "abc"}) == 120

    def test_get_max_extra_replicas_default(self) -> None:
        assert get_max_extra_replicas(None) == 5

    def test_get_max_extra_replicas_negative(self) -> None:
        """Negative value falls back to default."""
        assert get_max_extra_replicas({"maxExtraReplicas": -1}) == 5

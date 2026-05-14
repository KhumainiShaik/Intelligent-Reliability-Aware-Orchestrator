"""Unit tests for controller.reconciler helper logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import controller.reconciler as reconciler
from controller.reconciler import (
    ACTION_CANARY,
    ACTION_DELAY,
    ACTION_PRE_SCALE,
    ACTION_RL,
    ACTION_ROLLING,
    ACTION_RULE_BASED,
    _adaptive_pre_scale_extra_replicas,
    _apply_rollout_hints,
    _is_action_allowed,
    _is_fixed_baseline_action_set,
    _is_rule_based_action_set,
    _rule_based_decision,
    _v12_contextual_decision,
    reconcile,
)
from controller.snapshot import DecisionSnapshot


class TestRolloutHints:
    """Tests for optional rolloutHints policy overrides."""

    def test_spike_hint_prefers_prescale(self) -> None:
        """Spike-shaped traffic should steer low-stress delay decisions to pre-scale."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, reason = _apply_rollout_hints(
            ACTION_DELAY,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "spike"},
        )

        assert action == ACTION_PRE_SCALE
        assert overridden is True
        assert "trafficProfile=spike" in reason

    def test_reliability_objective_prefers_prescale_for_ramp(self) -> None:
        """Reliability-first ramp traffic should use pre-scale headroom."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, _ = _apply_rollout_hints(
            ACTION_ROLLING,
            snap,
            stress_score=0.2,
            rollout_hints={"trafficProfile": "ramp"},
        )

        assert action == ACTION_PRE_SCALE
        assert overridden is True

    def test_reliability_objective_prefers_prescale_for_steady(self) -> None:
        """Reliability-first steady traffic should still use deployment headroom."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, _ = _apply_rollout_hints(
            ACTION_ROLLING,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "steady"},
        )

        assert action == ACTION_PRE_SCALE
        assert overridden is True

    def test_latency_objective_uses_rolling_for_ramp(self) -> None:
        """Latency-oriented ramp traffic keeps the earlier rolling mapping."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, _ = _apply_rollout_hints(
            ACTION_PRE_SCALE,
            snap,
            stress_score=0.2,
            rollout_hints={"trafficProfile": "ramp", "objective": "latency"},
        )

        assert action == ACTION_ROLLING
        assert overridden is True

    def test_latency_objective_uses_canary_for_healthy_steady(self) -> None:
        """Latency-oriented steady low-stress traffic can use canary."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, _ = _apply_rollout_hints(
            ACTION_ROLLING,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "steady", "objective": "latency"},
        )

        assert action == ACTION_CANARY
        assert overridden is True

    def test_single_action_set_blocks_hint_override(self) -> None:
        """Fixed baseline action sets are not changed by advisory hints."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, reason = _apply_rollout_hints(
            ACTION_ROLLING,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "spike"},
            allowed_actions=[ACTION_ROLLING],
        )

        assert action == ACTION_ROLLING
        assert overridden is False
        assert reason == ""

    def test_fixed_delay_action_set_blocks_hint_override(self) -> None:
        """Fixed Delay remains Delay even when hints recommend Pre-Scale."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, reason = _apply_rollout_hints(
            ACTION_DELAY,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "spike", "objective": "reliability"},
            allowed_actions=[ACTION_DELAY],
        )

        assert action == ACTION_DELAY
        assert overridden is False
        assert reason == ""

    def test_fixed_canary_action_set_blocks_hint_override(self) -> None:
        """Fixed Canary remains Canary even when hints recommend Pre-Scale."""
        snap = DecisionSnapshot(error_rate=0.0)

        action, overridden, reason = _apply_rollout_hints(
            ACTION_CANARY,
            snap,
            stress_score=0.1,
            rollout_hints={"trafficProfile": "spike", "objective": "reliability"},
            allowed_actions=[ACTION_CANARY],
        )

        assert action == ACTION_CANARY
        assert overridden is False
        assert reason == ""


class TestActionSetModes:
    """Tests for final six-mode actionSet classification."""

    def test_all_final_modes_are_recognised(self) -> None:
        assert _is_action_allowed(ACTION_PRE_SCALE, [ACTION_RL]) is True
        assert _is_rule_based_action_set([ACTION_RULE_BASED]) is True
        for action in (ACTION_ROLLING, ACTION_CANARY, ACTION_PRE_SCALE, ACTION_DELAY):
            assert _is_fixed_baseline_action_set([action]) is True


class TestAdaptiveCapacity:
    """Tests for adaptive pre-scale capacity sizing."""

    def test_reliability_spike_uses_max_extra_replicas(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        extra, reason = _adaptive_pre_scale_extra_replicas(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "spike", "objective": "reliability"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert extra == 5
        assert "extra=5" in reason

    def test_reliability_steady_uses_moderate_extra_replicas(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        extra, _ = _adaptive_pre_scale_extra_replicas(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "steady", "objective": "reliability"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert extra == 4

    def test_live_pressure_uses_max_extra_replicas(self) -> None:
        snap = DecisionSnapshot(error_rate=0.02)

        extra, _ = _adaptive_pre_scale_extra_replicas(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "steady", "objective": "reliability"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert extra == 5

    def test_latency_objective_leaves_capacity_default(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        extra, reason = _adaptive_pre_scale_extra_replicas(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "spike", "objective": "latency"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert extra is None
        assert reason == ""


class TestRuleBasedBaseline:
    """Tests for the deterministic rule-based baseline selector."""

    def test_reliability_objective_uses_max_headroom(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        action, extra, reason = _rule_based_decision(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "ramp", "objective": "reliability"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 5
        assert "reliability" in reason

    def test_latency_ramp_uses_default_prescale_headroom(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        action, extra, reason = _rule_based_decision(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "ramp", "objective": "latency"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 3
        assert "ramp" in reason

    def test_extreme_stress_delays(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0, node_cpu_util=0.95)

        action, extra, reason = _rule_based_decision(
            snap,
            stress_score=0.2,
            rollout_hints={"trafficProfile": "spike", "objective": "reliability"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert action == ACTION_DELAY
        assert extra is None
        assert "extreme stress" in reason

    def test_live_hpa_pressure_uses_max_headroom(self) -> None:
        snap = DecisionSnapshot(hpa_desired_replicas=8, hpa_current_replicas=2)

        action, extra, reason = _rule_based_decision(
            snap,
            stress_score=0.02,
            rollout_hints={"trafficProfile": "steady", "objective": "latency"},
            guardrail_cfg={"maxExtraReplicas": 5},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 5
        assert "pressure" in reason


class TestV12ContextualPolicy:
    """Tests for the opt-in v12 contextual candidate policy."""

    def test_ramp_none_low_pressure_uses_rolling(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0, node_cpu_util=0.20)

        action, extra, reason = _v12_contextual_decision(
            snap,
            stress_score=0.02,
            rollout_hints={
                "trafficProfile": "ramp",
                "faultContext": "none",
                "objective": "reliability",
                "policyVariant": "v12-contextual",
            },
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert action == ACTION_ROLLING
        assert extra is None
        assert "ramp/none" in reason

    def test_v12_differs_from_rule_based_in_low_risk_context(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0, node_cpu_util=0.20)
        hints = {
            "trafficProfile": "ramp",
            "faultContext": "none",
            "objective": "reliability",
            "policyVariant": "v12-contextual",
        }

        v12_action, v12_extra, _ = _v12_contextual_decision(
            snap,
            stress_score=0.02,
            rollout_hints=hints,
            guardrail_cfg={"maxExtraReplicas": 7},
        )
        rule_action, rule_extra, _ = _rule_based_decision(
            snap,
            stress_score=0.02,
            rollout_hints=hints,
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert (v12_action, v12_extra) != (rule_action, rule_extra)
        assert (rule_action, rule_extra) == (ACTION_PRE_SCALE, 7)

    def test_ramp_none_pressure_uses_prescale_three(self) -> None:
        snap = DecisionSnapshot(hpa_desired_replicas=5, hpa_current_replicas=4)

        action, extra, _ = _v12_contextual_decision(
            snap,
            stress_score=0.02,
            rollout_hints={
                "trafficProfile": "ramp",
                "faultContext": "none",
                "objective": "reliability",
                "policyVariant": "v12-contextual",
            },
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 3

    def test_ramp_fault_uses_prescale_five(self) -> None:
        snap = DecisionSnapshot(error_rate=0.0)

        action, extra, _ = _v12_contextual_decision(
            snap,
            stress_score=0.02,
            rollout_hints={
                "trafficProfile": "ramp",
                "faultContext": "pod-kill",
                "objective": "reliability",
                "policyVariant": "v12-contextual",
            },
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 5

    def test_spike_fault_pressure_can_use_prescale_seven(self) -> None:
        snap = DecisionSnapshot(node_cpu_util=0.72)

        action, extra, _ = _v12_contextual_decision(
            snap,
            stress_score=0.12,
            rollout_hints={
                "trafficProfile": "spike",
                "faultContext": "network-latency",
                "objective": "reliability",
                "policyVariant": "v12-contextual",
            },
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert action == ACTION_PRE_SCALE
        assert extra == 7

    def test_extreme_pressure_delays(self) -> None:
        snap = DecisionSnapshot(node_cpu_util=0.95)

        action, extra, reason = _v12_contextual_decision(
            snap,
            stress_score=0.2,
            rollout_hints={
                "trafficProfile": "spike",
                "faultContext": "pod-kill",
                "objective": "reliability",
                "policyVariant": "v12-contextual",
            },
            guardrail_cfg={"maxExtraReplicas": 7},
        )

        assert action == ACTION_DELAY
        assert extra is None
        assert "delay-30" in reason


class TestFixedBaselineReconcile:
    """Tests that fixed baselines materialise unchanged."""

    def _install_registry(
        self,
        monkeypatch,
        default_config,
        snap: DecisionSnapshot,
        guardrail_action: str,
    ) -> SimpleNamespace:
        guardrails = MagicMock()
        guardrails.apply.return_value = (guardrail_action, True, "test override")
        registry = SimpleNamespace(
            cfg=default_config,
            snap_collector=MagicMock(collect=MagicMock(return_value=snap)),
            guardrails=guardrails,
            materialiser=MagicMock(),
            episode_logger=MagicMock(),
            custom_api=MagicMock(),
            policy_engine=None,
        )
        monkeypatch.setattr(reconciler, "_registry", registry)
        monkeypatch.setattr(reconciler, "_patch_status", MagicMock())
        return registry

    def test_fixed_delay_is_not_changed_by_guardrails_or_hints(
        self,
        monkeypatch,
        default_config,
    ) -> None:
        registry = self._install_registry(
            monkeypatch,
            default_config,
            DecisionSnapshot(node_cpu_util=0.95),
            ACTION_PRE_SCALE,
        )
        spec = {
            "targetRef": {"name": "workload"},
            "release": {"image": "repo", "tag": "v2"},
            "actionSet": [ACTION_DELAY],
            "rolloutHints": {"trafficProfile": "spike", "objective": "reliability"},
        }
        body = {"metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"}, "spec": spec}

        result = reconcile(spec, {}, body["metadata"], "ns", "oroll", "uid", body)

        assert result["chosenStrategy"] == ACTION_DELAY
        registry.guardrails.apply.assert_not_called()
        registry.materialiser.apply.assert_called_once_with(
            body,
            ACTION_DELAY,
            pre_scale_extra_replicas=None,
        )

    def test_fixed_canary_is_not_changed_by_guardrails_or_hints(
        self,
        monkeypatch,
        default_config,
    ) -> None:
        registry = self._install_registry(
            monkeypatch,
            default_config,
            DecisionSnapshot(error_rate=0.0),
            ACTION_PRE_SCALE,
        )
        spec = {
            "targetRef": {"name": "workload"},
            "release": {"image": "repo", "tag": "v2"},
            "actionSet": [ACTION_CANARY],
            "rolloutHints": {"trafficProfile": "spike", "objective": "reliability"},
        }
        body = {"metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"}, "spec": spec}

        result = reconcile(spec, {}, body["metadata"], "ns", "oroll", "uid", body)

        assert result["chosenStrategy"] == ACTION_CANARY
        registry.guardrails.apply.assert_not_called()
        registry.materialiser.apply.assert_called_once_with(
            body,
            ACTION_CANARY,
            pre_scale_extra_replicas=None,
        )

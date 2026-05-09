"""Unit tests for controller.stress."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from controller.snapshot import DecisionSnapshot
from controller.stress import Calculator

if TYPE_CHECKING:
    from controller.config import ControllerConfig


class TestCalculator:
    """Tests for the StressScore Calculator."""

    def test_healthy_cluster_low_score(self, healthy_snapshot: DecisionSnapshot) -> None:
        """A healthy snapshot produces a low stress score."""
        calc = Calculator()
        score = calc.compute(healthy_snapshot)
        assert 0.0 <= score <= 0.3, f"Expected low score, got {score}"

    def test_stressed_cluster_high_score(self, stressed_snapshot: DecisionSnapshot) -> None:
        """A stressed snapshot produces a high stress score."""
        calc = Calculator()
        score = calc.compute(stressed_snapshot)
        assert score >= 0.5, f"Expected high score, got {score}"

    def test_degraded_returns_fixed(self, degraded_snapshot: DecisionSnapshot) -> None:
        """Degraded snapshots return 0.7 (conservative assumption)."""
        calc = Calculator()
        score = calc.compute(degraded_snapshot)
        assert score == pytest.approx(0.7)

    def test_score_bounded_zero_one(self) -> None:
        """Score is always clamped to [0, 1]."""
        calc = Calculator()
        # Extreme values
        extreme = DecisionSnapshot(
            rps=10000.0,
            p95_latency_ms=5000.0,
            error_rate=1.0,
            pending_pods=100,
            node_cpu_util=1.0,
            node_mem_util=1.0,
            hpa_desired_replicas=50,
            hpa_current_replicas=5,
        )
        score = calc.compute(extreme)
        assert 0.0 <= score <= 1.0

    def test_zero_signals(self) -> None:
        """All-zero inputs produce score 0."""
        calc = Calculator()
        snap = DecisionSnapshot()
        score = calc.compute(snap)
        assert score == pytest.approx(0.0)

    def test_ewma_trend_boost(self) -> None:
        """Rising latency above EWMA triggers a trend bonus."""
        calc = Calculator()
        # First call initialises EWMA
        snap1 = DecisionSnapshot(p95_latency_ms=100.0, rps=100.0)
        score1 = calc.compute(snap1)

        # Sharp latency spike
        snap2 = DecisionSnapshot(p95_latency_ms=400.0, rps=100.0)
        score2 = calc.compute(snap2)

        assert score2 > score1, "Trend boost should raise score on latency spike"

    def test_from_config(self, default_config: ControllerConfig) -> None:
        """Calculator.from_config maps config fields correctly."""
        calc = Calculator.from_config(default_config)
        assert calc.weight_latency == default_config.stress_weight_latency
        assert calc.alpha == default_config.stress_ewma_alpha
        assert calc.latency_ceiling_ms == default_config.latency_ceiling_ms

    def test_custom_weights(self) -> None:
        """Custom weights change the score proportionally."""
        snap = DecisionSnapshot(
            p95_latency_ms=250.0,
            error_rate=0.0,
            node_cpu_util=0.0,
            node_mem_util=0.0,
        )
        # Heavy latency weight
        high_lat = Calculator(
            weight_latency=0.80,
            weight_error_rate=0.05,
            weight_pending_pod=0.05,
            weight_cpu=0.05,
            weight_mem=0.025,
            weight_hpa_gap=0.025,
        )
        # Low latency weight
        low_lat = Calculator(
            weight_latency=0.05,
            weight_error_rate=0.40,
            weight_pending_pod=0.15,
            weight_cpu=0.20,
            weight_mem=0.10,
            weight_hpa_gap=0.10,
        )
        score_high = high_lat.compute(snap)
        score_low = low_lat.compute(snap)
        assert score_high > score_low, "Higher latency weight should give higher score"

    def test_custom_ceilings(self) -> None:
        """Custom ceilings affect normalisation."""
        snap = DecisionSnapshot(p95_latency_ms=250.0)
        # Default ceiling 500 → latency_stress = 0.5
        calc_default = Calculator()
        # Ceiling 250 → latency_stress = 1.0
        calc_low = Calculator(latency_ceiling_ms=250.0)

        s1 = calc_default.compute(snap)
        s2 = calc_low.compute(snap)
        assert s2 > s1, "Lower ceiling should increase normalised score"

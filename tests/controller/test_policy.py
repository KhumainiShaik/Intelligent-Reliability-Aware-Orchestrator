"""Unit tests for controller.policy."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from controller.policy import (
    ALL_ACTIONS,
    Engine,
    PolicyArtifact,
    _digitise,
)
from controller.snapshot import DecisionSnapshot

if TYPE_CHECKING:
    from pathlib import Path


class TestPolicyArtifact:
    """Tests for PolicyArtifact loading."""

    def test_from_file(self, policy_artifact_dir: Path) -> None:
        """from_file() loads and parses a JSON artifact."""
        artifact = PolicyArtifact.from_file(policy_artifact_dir / "policy_artifact.json")
        assert artifact.version == "test-v1"
        assert len(artifact.actions) == 4
        assert len(artifact.q_table) > 0
        assert len(artifact.features) == 8

    def test_from_file_missing(self, tmp_path: Path) -> None:
        """from_file() raises on missing file."""
        with pytest.raises(FileNotFoundError):
            PolicyArtifact.from_file(tmp_path / "nonexistent.json")


class TestDigitise:
    """Tests for bin assignment."""

    def test_below_first_edge(self) -> None:
        assert _digitise(5.0, [10.0, 20.0]) == 0

    def test_between_edges(self) -> None:
        assert _digitise(15.0, [10.0, 20.0]) == 1

    def test_above_last_edge(self) -> None:
        assert _digitise(25.0, [10.0, 20.0]) == 2

    def test_at_edge(self) -> None:
        """Value exactly at edge falls into the next bin."""
        assert _digitise(10.0, [10.0, 20.0]) == 1

    def test_empty_bins(self) -> None:
        assert _digitise(5.0, []) == 0


class TestEngine:
    """Tests for the policy Engine."""

    def test_select_action(self, policy_artifact_dir: Path) -> None:
        """Engine selects the best action from Q-table."""
        engine = Engine(str(policy_artifact_dir))
        snap = DecisionSnapshot(
            rps=100.0,
            p95_latency_ms=50.0,
            error_rate=0.001,
            pending_pods=0,
            node_cpu_util=0.3,
            node_mem_util=0.3,
            hpa_desired_replicas=3,
            hpa_current_replicas=3,
        )
        action, version = engine.select_action(snap, stress_score=0.1)
        assert action in ALL_ACTIONS
        assert version == "test-v1"

    def test_version_property(self, policy_artifact_dir: Path) -> None:
        """engine.version returns artifact version."""
        engine = Engine(str(policy_artifact_dir))
        assert engine.version == "test-v1"

    def test_v11_no_forecast_low_stress_selects_prescale(self) -> None:
        """The default no-forecast policy should not require a forecast signal."""
        policy_dir = Path("artifacts/v11_no_forecast")
        engine = Engine(str(policy_dir))
        snap = DecisionSnapshot(
            rps=100.0,
            p95_latency_ms=40.0,
            error_rate=0.0,
            pending_pods=0,
            node_cpu_util=0.0,
            node_mem_util=0.2,
            stress_forecast=0.0,
        )
        action, version = engine.select_action(snap, stress_score=0.05)
        assert action == "pre-scale"
        assert version == "v11-no-forecast"

    def test_empty_q_table_raises(self, tmp_path: Path) -> None:
        """Empty Q-table raises ValueError on construction."""
        import json

        artifact = {
            "version": "empty",
            "features": [{"name": "rps", "min_val": 0, "max_val": 100, "num_bins": 2}],
            "bins": {"rps": [50.0]},
            "q_table": {},
            "actions": ["delay"],
        }
        (tmp_path / "policy_artifact.json").write_text(json.dumps(artifact))
        with pytest.raises(ValueError, match="Empty Q-table"):
            Engine(str(tmp_path))

"""Unit tests for controller.episode."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from controller.episode import EpisodeLogger, EpisodeRecord, OutcomeMetrics

if TYPE_CHECKING:
    from pathlib import Path


class TestEpisodeRecord:
    """Tests for EpisodeRecord dataclass."""

    def test_defaults(self) -> None:
        """Default record has empty strings and no outcome."""
        rec = EpisodeRecord()
        assert rec.run_id == ""
        assert rec.outcome is None

    def test_fields_set(self) -> None:
        rec = EpisodeRecord(
            run_id="abc-123",
            namespace="prod",
            name="my-rollout",
            chosen_action="canary",
            stress_score=0.45,
        )
        assert rec.namespace == "prod"
        assert rec.stress_score == 0.45


class TestOutcomeMetrics:
    """Tests for OutcomeMetrics dataclass."""

    def test_defaults(self) -> None:
        om = OutcomeMetrics()
        assert om.success is False
        assert om.computed_cost == 0.0


class TestEpisodeLogger:
    """Tests for EpisodeLogger file writing."""

    def test_write_creates_file(self, tmp_path: Path) -> None:
        """Writing a record creates a JSON file."""
        log = EpisodeLogger(str(tmp_path))
        record = EpisodeRecord(
            run_id="test-run-12345678",
            timestamp="20240101_120000",
            namespace="default",
            name="my-rollout",
            chosen_action="canary",
        )
        log.write(record)

        files = list(tmp_path.glob("episode_*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text())
        assert data["run_id"] == "test-run-12345678"
        assert data["chosen_action"] == "canary"

    def test_write_multiple(self, tmp_path: Path) -> None:
        """Multiple writes create separate files."""
        log = EpisodeLogger(str(tmp_path))
        for i in range(3):
            record = EpisodeRecord(
                run_id=f"run-{i:08d}",
                timestamp=f"20240101_12000{i}",
            )
            log.write(record)

        files = list(tmp_path.glob("episode_*.json"))
        assert len(files) == 3

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Logger creates the output directory if it doesn't exist."""
        nested = tmp_path / "a" / "b" / "c"
        EpisodeLogger(str(nested))
        assert nested.exists()

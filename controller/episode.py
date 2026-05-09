"""
Episode Logger — structured JSON episode records.

Writes one file per reconciliation event for offline analysis and
RL reward labelling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class OutcomeMetrics:
    """Post-rollout measurements (populated after rollout completes)."""

    slo_violation_seconds: float = 0.0
    p95_impact: float = 0.0
    p99_impact: float = 0.0
    error_rate_peak: float = 0.0
    replica_seconds: float = 0.0
    success: bool = False
    rollback: bool = False
    duration_seconds: float = 0.0
    time_to_recovery: float = 0.0
    computed_cost: float = 0.0


@dataclass
class EpisodeRecord:
    """A single rollout episode for logging and evaluation."""

    run_id: str = ""
    timestamp: str = ""
    namespace: str = ""
    name: str = ""
    policy_version: str = ""
    decision_snapshot: dict = field(default_factory=dict)
    stress_score: float = 0.0
    chosen_action: str = ""
    original_action: str = ""
    guardrail_override: bool = False
    override_reason: str = ""
    outcome: dict | None = None


class EpisodeLogger:
    """Writes episode records as structured JSON files."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, record: EpisodeRecord) -> None:
        """Persist an episode record to disk."""
        ts = record.timestamp or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        run_prefix = record.run_id[:8] if len(record.run_id) >= 8 else record.run_id
        filename = f"episode_{ts}_{run_prefix}.json"
        path = self._dir / filename

        data = asdict(record)
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.debug("Episode written: %s", path)

    def read_all(self) -> list[dict]:
        """Read all episode records from the log directory."""
        records: list[dict] = []
        for entry in sorted(self._dir.iterdir()):
            if entry.suffix != ".json" or not entry.is_file():
                continue
            try:
                records.append(json.loads(entry.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return records

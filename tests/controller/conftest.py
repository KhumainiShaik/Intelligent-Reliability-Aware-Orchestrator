"""Shared fixtures for controller tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from controller.config import ControllerConfig
from controller.snapshot import DecisionSnapshot

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures: configuration
# ---------------------------------------------------------------------------


@pytest.fixture()
def default_config() -> ControllerConfig:
    """Return a :class:`ControllerConfig` with safe test defaults."""
    return ControllerConfig(
        prometheus_url="http://localhost:9090",
        prometheus_timeout=2.0,
        policy_path="/tmp/test-policy",
        episode_log_path="/tmp/test-episodes",
        log_level="DEBUG",
        log_format="text",
    )


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all controller-related env vars so tests start clean."""
    env_keys = [
        "PROMETHEUS_URL",
        "PROMETHEUS_TIMEOUT",
        "POLICY_PATH",
        "EPISODE_LOG_PATH",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "STRESS_WEIGHT_LATENCY",
        "STRESS_WEIGHT_ERROR",
        "STRESS_WEIGHT_PENDING",
        "STRESS_WEIGHT_CPU",
        "STRESS_WEIGHT_MEM",
        "STRESS_WEIGHT_HPA_GAP",
        "STRESS_EWMA_ALPHA",
        "LATENCY_CEILING_MS",
        "ERROR_RATE_CEILING",
        "PENDING_PODS_CEILING",
        "MAX_DELAY_SECONDS",
        "MAX_EXTRA_REPLICAS",
        "MAX_ROLLOUT_TIME_SECONDS",
        "DEFAULT_DELAY_SECONDS",
        "CANARY_PAUSE_DURATION",
        "RETRY_MAX_ATTEMPTS",
        "RETRY_BACKOFF_FACTOR",
    ]
    for key in env_keys:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Fixtures: snapshots
# ---------------------------------------------------------------------------


@pytest.fixture()
def healthy_snapshot() -> DecisionSnapshot:
    """Snapshot representing a healthy cluster state."""
    return DecisionSnapshot(
        rps=100.0,
        p95_latency_ms=50.0,
        p99_latency_ms=80.0,
        error_rate=0.001,
        pending_pods=0,
        restart_count=0,
        node_cpu_util=0.3,
        node_mem_util=0.4,
        hpa_desired_replicas=3,
        hpa_current_replicas=3,
        target_replicas=3,
    )


@pytest.fixture()
def stressed_snapshot() -> DecisionSnapshot:
    """Snapshot representing a heavily loaded cluster."""
    return DecisionSnapshot(
        rps=500.0,
        p95_latency_ms=450.0,
        p99_latency_ms=600.0,
        error_rate=0.04,
        pending_pods=8,
        restart_count=3,
        node_cpu_util=0.92,
        node_mem_util=0.88,
        hpa_desired_replicas=10,
        hpa_current_replicas=5,
        target_replicas=5,
    )


@pytest.fixture()
def degraded_snapshot() -> DecisionSnapshot:
    """Snapshot with degraded flag set (Prometheus unreachable)."""
    return DecisionSnapshot(degraded=True)


# ---------------------------------------------------------------------------
# Fixtures: policy artifact
# ---------------------------------------------------------------------------


@pytest.fixture()
def policy_artifact_dict() -> dict:
    """Minimal valid policy artifact for testing."""
    return {
        "version": "test-v1",
        "features": [
            {"name": "rps", "min_val": 0, "max_val": 1000, "num_bins": 3},
            {"name": "p95_latency", "min_val": 0, "max_val": 500, "num_bins": 3},
            {"name": "error_rate", "min_val": 0, "max_val": 0.1, "num_bins": 2},
            {"name": "pending_pods", "min_val": 0, "max_val": 10, "num_bins": 2},
            {"name": "cpu_util", "min_val": 0, "max_val": 1, "num_bins": 2},
            {"name": "mem_util", "min_val": 0, "max_val": 1, "num_bins": 2},
            {"name": "hpa_gap", "min_val": 0, "max_val": 5, "num_bins": 2},
            {"name": "stress_score", "min_val": 0, "max_val": 1, "num_bins": 3},
        ],
        "bins": {
            "rps": [250.0, 500.0, 750.0],
            "p95_latency": [100.0, 250.0, 400.0],
            "error_rate": [0.02, 0.05],
            "pending_pods": [3.0, 7.0],
            "cpu_util": [0.5, 0.8],
            "mem_util": [0.5, 0.8],
            "hpa_gap": [1.0, 3.0],
            "stress_score": [0.3, 0.6, 0.8],
        },
        "q_table": {
            "0_0_0_0_0_0_0_0": [0.1, 0.2, 0.3, 0.9],  # rolling wins
            "3_3_1_1_1_1_1_2": [0.9, 0.1, 0.5, 0.2],  # delay wins
        },
        "actions": ["delay", "pre-scale", "canary", "rolling"],
    }


@pytest.fixture()
def policy_artifact_dir(
    policy_artifact_dict: dict,
    tmp_path: Path,
) -> Path:
    """Write a policy artifact to a temp directory and return the dir path."""
    artifact_file = tmp_path / "policy_artifact.json"
    artifact_file.write_text(json.dumps(policy_artifact_dict))
    return tmp_path

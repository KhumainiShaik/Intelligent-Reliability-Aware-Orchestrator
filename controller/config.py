"""
Centralised configuration for the OrchestratedRollout controller.

All tunables are exposed via environment variables with safe defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControllerConfig:
    """Immutable controller-wide configuration loaded from environment."""

    # Prometheus
    prometheus_url: str = "http://prometheus.monitoring.svc.cluster.local:9090"
    prometheus_timeout: float = 10.0

    # Policy engine
    policy_path: str = "/policy/artifact"

    # Episode logger
    episode_log_path: str = "/episodes"

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # StressScore weights (must sum to ~1.0)
    stress_weight_latency: float = 0.25
    stress_weight_error: float = 0.25
    stress_weight_pending: float = 0.15
    stress_weight_cpu: float = 0.15
    stress_weight_mem: float = 0.10
    stress_weight_hpa_gap: float = 0.10

    # StressScore EWMA parameters
    stress_ewma_alpha: float = 0.3

    # StressScore normalisation
    latency_ceiling_ms: float = 500.0
    error_rate_ceiling: float = 0.05
    pending_pods_ceiling: float = 10.0

    # Guardrails defaults
    max_delay_seconds: int = 120
    max_extra_replicas: int = 5
    max_rollout_time_seconds: int = 600

    # Canary steps (materialiser)
    canary_steps_weights: tuple[int, ...] = (10, 25, 50, 100)
    canary_pause_duration: str = "30s"
    default_delay_seconds: int = 120

    # Retry parameters
    retry_max_attempts: int = 3
    retry_backoff_factor: float = 0.5


def _env(key: str, default: str) -> str:
    """Read an env var, stripping whitespace."""
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    """Read an env var as float, falling back to *default*."""
    raw = os.environ.get(key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r — using default %s", key, raw, default)
        return default


def _env_int(key: str, default: int) -> int:
    """Read an env var as int, falling back to *default*."""
    raw = os.environ.get(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r — using default %s", key, raw, default)
        return default


def load_config() -> ControllerConfig:
    """Build an immutable :class:`ControllerConfig` from environment variables."""
    cfg = ControllerConfig(
        prometheus_url=_env("PROMETHEUS_URL", ControllerConfig.prometheus_url),
        prometheus_timeout=_env_float("PROMETHEUS_TIMEOUT", ControllerConfig.prometheus_timeout),
        policy_path=_env("POLICY_PATH", ControllerConfig.policy_path),
        episode_log_path=_env("EPISODE_LOG_PATH", ControllerConfig.episode_log_path),
        log_level=_env("LOG_LEVEL", ControllerConfig.log_level).upper(),
        log_format=_env("LOG_FORMAT", ControllerConfig.log_format),
        # Stress weights
        stress_weight_latency=_env_float(
            "STRESS_WEIGHT_LATENCY", ControllerConfig.stress_weight_latency
        ),
        stress_weight_error=_env_float("STRESS_WEIGHT_ERROR", ControllerConfig.stress_weight_error),
        stress_weight_pending=_env_float(
            "STRESS_WEIGHT_PENDING", ControllerConfig.stress_weight_pending
        ),
        stress_weight_cpu=_env_float("STRESS_WEIGHT_CPU", ControllerConfig.stress_weight_cpu),
        stress_weight_mem=_env_float("STRESS_WEIGHT_MEM", ControllerConfig.stress_weight_mem),
        stress_weight_hpa_gap=_env_float(
            "STRESS_WEIGHT_HPA_GAP", ControllerConfig.stress_weight_hpa_gap
        ),
        stress_ewma_alpha=_env_float("STRESS_EWMA_ALPHA", ControllerConfig.stress_ewma_alpha),
        latency_ceiling_ms=_env_float("LATENCY_CEILING_MS", ControllerConfig.latency_ceiling_ms),
        error_rate_ceiling=_env_float("ERROR_RATE_CEILING", ControllerConfig.error_rate_ceiling),
        pending_pods_ceiling=_env_float(
            "PENDING_PODS_CEILING", ControllerConfig.pending_pods_ceiling
        ),
        # Guardrails
        max_delay_seconds=_env_int("MAX_DELAY_SECONDS", ControllerConfig.max_delay_seconds),
        max_extra_replicas=_env_int("MAX_EXTRA_REPLICAS", ControllerConfig.max_extra_replicas),
        max_rollout_time_seconds=_env_int(
            "MAX_ROLLOUT_TIME_SECONDS", ControllerConfig.max_rollout_time_seconds
        ),
        # Canary
        default_delay_seconds=_env_int(
            "DEFAULT_DELAY_SECONDS", ControllerConfig.default_delay_seconds
        ),
        canary_pause_duration=_env("CANARY_PAUSE_DURATION", ControllerConfig.canary_pause_duration),
        # Retry
        retry_max_attempts=_env_int("RETRY_MAX_ATTEMPTS", ControllerConfig.retry_max_attempts),
        retry_backoff_factor=_env_float(
            "RETRY_BACKOFF_FACTOR", ControllerConfig.retry_backoff_factor
        ),
    )
    logger.info(
        "Controller configuration loaded: prometheus=%s, log_level=%s",
        cfg.prometheus_url,
        cfg.log_level,
    )
    return cfg

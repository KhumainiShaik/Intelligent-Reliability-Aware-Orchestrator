"""
Centralised configuration for the OrchestratedRollout controller.

All tunables are exposed via environment variables with safe defaults.
Immutable after construction — thread-safe, testable, no global state.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ControllerConfig:
    """Immutable controller-wide configuration loaded from environment."""

    #Prometheus
    prometheus_url: str = "http://prometheus.monitoring.svc.cluster.local:9090"
    prometheus_timeout: float = 10.0

    #Policy engine 
    policy_path: str = "/policy/artifact"

    #Episode logger
    episode_log_path: str = "/episodes"

    #Logging
    log_level: str = "INFO"
    log_format: str = "json"


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
        prometheus_url=_env("PROMETHEUS_URL",
                            ControllerConfig.prometheus_url),
        prometheus_timeout=_env_float("PROMETHEUS_TIMEOUT",
                                      ControllerConfig.prometheus_timeout),
        policy_path=_env("POLICY_PATH",
                         ControllerConfig.policy_path),
        episode_log_path=_env("EPISODE_LOG_PATH",
                              ControllerConfig.episode_log_path),
        log_level=_env("LOG_LEVEL", ControllerConfig.log_level).upper(),
        log_format=_env("LOG_FORMAT", ControllerConfig.log_format),
    )
    logger.info("Controller configuration loaded: prometheus=%s, log_level=%s",
                cfg.prometheus_url, cfg.log_level)
    return cfg

#!/usr/bin/env python3
"""
Entry point for the OrchestratedRollout controller.

Runs kopf as the Kubernetes operator framework, watching
OrchestratedRollout CRs and orchestrating the reconciliation pipeline.

Configuration is via environment variables — see ``controller.config``
for the full list with defaults.
"""

from __future__ import annotations

import json
import logging
import sys


class _JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production use."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, default=str)


def main() -> None:
    from controller.config import load_config

    cfg = load_config()

    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)

    if cfg.log_format == "json":
        handler.setFormatter(_JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.handlers = [handler]

    logger = logging.getLogger("controller.main")
    logger.info("Starting orchestrated-rollout controller (Python/kopf)")
    logger.info("  PROMETHEUS_URL  = %s", cfg.prometheus_url)
    logger.info("  POLICY_PATH     = %s", cfg.policy_path)
    logger.info("  EPISODE_LOG_PATH= %s", cfg.episode_log_path)

    # Import reconciler module to register kopf handlers
    # Run kopf
    import kopf

    import controller.reconciler  # noqa: F401

    kopf.run(
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8081/healthz",
    )


if __name__ == "__main__":
    main()

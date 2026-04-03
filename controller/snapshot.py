"""
Decision Snapshot collector.

Queries Prometheus for service-health, cluster-pressure, and HPA context
signals, producing a DecisionSnapshot dataclass consumed by downstream modules.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from controller.config import ControllerConfig

logger = logging.getLogger(__name__)


@dataclass
class DecisionSnapshot:
    """All signals collected at deploy time for strategy selection."""

    # Service health (Prometheus)
    rps: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    error_rate: float = 0.0

    # Cluster pressure (Kubernetes / kube-state-metrics)
    pending_pods: int = 0
    restart_count: int = 0
    node_cpu_util: float = 0.0
    node_mem_util: float = 0.0

    # Autoscaling context
    hpa_desired_replicas: int = 0
    hpa_current_replicas: int = 0

    # Rollout context
    target_replicas: int = 0
    warmup_class: str = ""

    # Traffic trend (EWMA slope)
    rps_trend: float = 0.0

    # Stress forecast (from external forecasting service, or 0 if unavailable)
    # In testbed: provided by experiment harness (ground truth from k6 script)
    # In production: provided by workload forecasting service or defaults to 0
    stress_forecast: float = 0.0

    # Degraded flag — set when Prometheus is unreachable
    degraded: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def degraded_snapshot() -> DecisionSnapshot:
    """Return a snapshot with only the degraded flag set."""
    return DecisionSnapshot(degraded=True)


class Collector:
    """Gathers a DecisionSnapshot from Prometheus and the Kubernetes API."""

    def __init__(
        self,
        prometheus_url: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        if not prometheus_url:
            raise ValueError("prometheus_url is required")
        self._prom = prometheus_url.rstrip("/")
        self._timeout = timeout

        # Resilient HTTP session with retry + backoff
        self._session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    @classmethod
    def from_config(cls, cfg: ControllerConfig) -> Collector:
        """Factory: build a Collector from a :class:`ControllerConfig`."""
        return cls(
            prometheus_url=cfg.prometheus_url,
            timeout=cfg.prometheus_timeout,
            max_retries=cfg.retry_max_attempts,
            backoff_factor=cfg.retry_backoff_factor,
        )

    def collect(self, namespace: str, target_name: str) -> DecisionSnapshot:
        """Collect the current decision snapshot for *target_name*."""
        snap = DecisionSnapshot()
        errors: list[str] = []

        # Scope workload-level metrics to the rollout target.
        # We rely on the Prometheus relabeling that sets `kubernetes_pod_name`.
        # This avoids mixing Go + ML workload metrics in the same namespace.
        target_pod_re = f"{target_name}-.*"

        # Service health
        # Prefer inference traffic only: this aligns RPS/error_rate with what k6 drives.
        # Fall back to a longer window when the time series is new (rate([2m]) can return
        # no data if Prometheus hasn't scraped two samples yet).
        rps_queries = [
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[2m]))',
                "inference_rps_2m",
            ),
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[5m]))',
                "inference_rps_5m",
            ),
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}"}}[2m]))',
                "all_rps_2m",
            ),
        ]
        last_rps_err: str | None = None
        for query, _label in rps_queries:
            snap.rps, last_rps_err = self._query_scalar(query)
            if not last_rps_err:
                break
        if last_rps_err:
            errors.append(f"RPS: {last_rps_err}")

        p95, err = self._query_scalar(
            f"histogram_quantile(0.95, sum(rate(workload_request_duration_seconds_bucket"
            f'{{kubernetes_namespace="{namespace}",kubernetes_pod_name=~"{target_pod_re}",'
            f'endpoint="inference"}}[2m])) by (le))'
        )
        if err:
            errors.append(f"p95: {err}")
        else:
            snap.p95_latency_ms = p95 * 1000.0

        p99, err = self._query_scalar(
            f"histogram_quantile(0.99, sum(rate(workload_request_duration_seconds_bucket"
            f'{{kubernetes_namespace="{namespace}",kubernetes_pod_name=~"{target_pod_re}",'
            f'endpoint="inference"}}[2m])) by (le))'
        )
        if err:
            errors.append(f"p99: {err}")
        else:
            snap.p99_latency_ms = p99 * 1000.0

        error_rate_queries = [
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference",status=~"5.."}}[2m]))'
                f' / sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[2m]))',
                "inference_err_2m",
            ),
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference",status=~"5.."}}[5m]))'
                f' / sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[5m]))',
                "inference_err_5m",
            ),
            (
                f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}",status=~"5.."}}[2m]))'
                f' / sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
                f'kubernetes_pod_name=~"{target_pod_re}"}}[2m]))',
                "all_err_2m",
            ),
        ]
        last_err_rate_err: str | None = None
        for query, _label in error_rate_queries:
            error_rate, last_err_rate_err = self._query_scalar(query)
            if not last_err_rate_err:
                snap.error_rate = error_rate
                break
        if last_err_rate_err:
            errors.append(f"error_rate: {last_err_rate_err}")

        # Cluster pressure
        pending, err = self._query_scalar('sum(kube_pod_status_phase{phase="Pending"})')
        if err:
            errors.append(f"pending_pods: {err}")
        else:
            snap.pending_pods = int(pending)

        restarts, err = self._query_scalar(
            f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}})'
        )
        if err:
            errors.append(f"restarts: {err}")
        else:
            snap.restart_count = int(restarts)

        cpu, err = self._query_scalar('avg(1 - rate(node_cpu_seconds_total{mode="idle"}[2m]))')
        if err:
            errors.append(f"cpu: {err}")
        else:
            snap.node_cpu_util = cpu

        mem, err = self._query_scalar(
            "avg(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"
        )
        if err:
            errors.append(f"mem: {err}")
        else:
            snap.node_mem_util = mem

        # HPA context
        hpa_name = f"{target_name}-hpa"
        desired, err = self._query_scalar(
            f"kube_horizontalpodautoscaler_status_desired_replicas"
            f'{{namespace="{namespace}",horizontalpodautoscaler="{hpa_name}"}}'
        )
        if err:
            errors.append(f"hpa_desired: {err}")
        else:
            snap.hpa_desired_replicas = int(desired)

        current, err = self._query_scalar(
            f"kube_horizontalpodautoscaler_status_current_replicas"
            f'{{namespace="{namespace}",horizontalpodautoscaler="{hpa_name}"}}'
        )
        if err:
            errors.append(f"hpa_current: {err}")
        else:
            snap.hpa_current_replicas = int(current)

        # RPS trend (slope) -- approximated via difference of short vs long windows
        rps_5m, err = self._query_scalar(
            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'
            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[5m]))'
        )
        if not err and snap.rps > 0:
            snap.rps_trend = (snap.rps - rps_5m) / max(snap.rps, 1.0)

        # Stress forecast derived from RPS velocity.
        # A strong positive rps_trend (short-window RPS >> 5-min average) indicates
        # that load is rising rapidly — a spike is imminent or in progress.
        # This signal activates the forecast-conditioned branches of the v10b policy,
        # which choose canary/delay instead of pre-scale when a spike is incoming.
        # Threshold 0.3: meaningful rise (30%+ above recent baseline) triggers forecast.
        if snap.rps_trend >= 0.3:
            snap.stress_forecast = min(snap.rps_trend, 1.0)
        elif snap.rps_trend > 0.0:
            snap.stress_forecast = snap.rps_trend
        else:
            snap.stress_forecast = 0.0

        logger.debug(
            "RPS trend=%.3f → stress_forecast=%.3f",
            snap.rps_trend,
            snap.stress_forecast,
        )

        # Mark degraded if too many individual queries failed
        if len(errors) > 3:
            snap.degraded = True
            logger.warning("snapshot degraded — %d query failures: %s", len(errors), errors)

        return snap

    def _query_scalar(self, query: str) -> tuple[float, str | None]:
        """Execute a Prometheus instant query and return (value, error_msg)."""
        try:
            resp = self._session.get(
                f"{self._prom}/api/v1/query",
                params={"query": query},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()

            if payload.get("status") != "success":
                return 0.0, f"non-success status: {payload.get('status')}"

            results = payload.get("data", {}).get("result", [])
            if not results:
                return 0.0, f"no data for query: {query[:80]}"

            value_pair = results[0].get("value", [])
            if len(value_pair) < 2:
                return 0.0, "unexpected value format"

            return float(value_pair[1]), None

        except requests.RequestException as exc:
            return 0.0, str(exc)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return 0.0, str(exc)

"""
StressScore computation.

Computes a scalar stress score ∈ [0, 1] from a DecisionSnapshot using
weighted normalisation of individual signals plus EWMA trend detection.

Method selected: **Short-horizon trend model (EWMA + change detection)**

Rationale

The architecture offers two approaches for StressScore:
    (a) schedule-aware stress flag — an external experiment harness sets a
        binary/ordinal stress level before each trial;
    (b) short-horizon trend model — the controller itself estimates stress
        from live Prometheus and Kubernetes signals.

Option (b) is used because:
  1. **Autonomy** — the controller must make a decision at deploy time
     without relying on an external harness flagging the cluster state.
     In production there is no experiment harness; the controller must
     self-assess stress from observable signals.
  2. **Sensitivity** -- EWMA (alpha = 0.3) with a 20 % deviation threshold
     detects latency trends within 2-3 scrape intervals (~30-45 s),
     which is fast enough for a deploy-time snapshot yet smooth enough
     to avoid noise-driven over-reaction.
  3. **Interpretability** — each signal maps to a normalised [0, 1]
     sub-score with explicit weights that sum to 1.0, making the final
     StressScore directly explainable in episode logs and publications.
  4. **Reproducibility** — the same Prometheus queries + deterministic
     weight vector produce the same score given the same cluster state,
     which satisfies the evaluation requirement for repeated trials.

The schedule-aware stress flag is still exposed via the experiment harness
(scripts/run_experiment.sh injects `CLUSTER_PRESSURE` labels).  It is used
in *evaluation labelling* to bucket episodes by stress tier, but is **not**
fed into the StressScore computation itself.

Signal weights

    latency      (p95)        0.25   — primary SLO indicator
    error_rate   (5xx ratio)  0.25   — primary reliability indicator
    pending_pods              0.15   — scheduling pressure
    node_cpu                  0.15   — compute headroom
    node_mem                  0.10   — memory headroom
    hpa_gap                   0.10   — autoscaler lag

EWMA trend boost

If the current p95 latency exceeds the EWMA by > 20 %, a capped bonus
(up to +0.20) is added to the score.  This makes the controller more
conservative when latency is actively *rising*, even if the absolute
level is still moderate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.config import ControllerConfig
    from controller.snapshot import DecisionSnapshot


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class Calculator:
    """Weighted StressScore calculator with EWMA trend detection.

    Each instance maintains its own EWMA state, so create one per CR
    to avoid cross-contamination when multiple CRs are reconciled
    concurrently.
    """

    # Signal weights (must sum to ~1)
    weight_latency: float = 0.25
    weight_error_rate: float = 0.25
    weight_pending_pod: float = 0.15
    weight_cpu: float = 0.15
    weight_mem: float = 0.10
    weight_hpa_gap: float = 0.10

    # Normalisation ceilings
    latency_ceiling_ms: float = 500.0
    error_rate_ceiling: float = 0.05
    pending_pods_ceiling: float = 10.0

    # EWMA parameters
    alpha: float = 0.3
    _ewma_latency: float = field(default=0.0, init=False, repr=False)
    _ewma_rps: float = field(default=0.0, init=False, repr=False)
    _initialised: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_config(cls, cfg: ControllerConfig) -> Calculator:
        """Factory: build a Calculator from a :class:`ControllerConfig`."""
        return cls(
            weight_latency=cfg.stress_weight_latency,
            weight_error_rate=cfg.stress_weight_error,
            weight_pending_pod=cfg.stress_weight_pending,
            weight_cpu=cfg.stress_weight_cpu,
            weight_mem=cfg.stress_weight_mem,
            weight_hpa_gap=cfg.stress_weight_hpa_gap,
            latency_ceiling_ms=cfg.latency_ceiling_ms,
            error_rate_ceiling=cfg.error_rate_ceiling,
            pending_pods_ceiling=cfg.pending_pods_ceiling,
            alpha=cfg.stress_ewma_alpha,
        )

    def compute(self, snap: DecisionSnapshot) -> float:
        """Return StressScore ∈ [0, 1] for the given snapshot."""
        if snap.degraded:
            # Conservative: assume moderate-high stress when degraded
            return 0.7

        # Guard: histogram_quantile returns NaN when there are no observations yet
        # (e.g. brand-new deployment).  Treat as 0 (no latency evidence) rather
        # than letting NaN propagate and corrupt the weighted sum.
        p95_safe = snap.p95_latency_ms if math.isfinite(snap.p95_latency_ms) else 0.0

        # Normalise each signal to [0, 1]
        latency_stress = self._normalise_latency(p95_safe)
        error_stress = self._normalise_error_rate(snap.error_rate)
        pending_stress = self._normalise_pending_pods(snap.pending_pods)
        cpu_stress = _clamp(snap.node_cpu_util)
        mem_stress = _clamp(snap.node_mem_util)
        hpa_gap_stress = self._normalise_hpa_gap(
            snap.hpa_desired_replicas, snap.hpa_current_replicas
        )

        # Weighted combination
        score = (
            self.weight_latency * latency_stress
            + self.weight_error_rate * error_stress
            + self.weight_pending_pod * pending_stress
            + self.weight_cpu * cpu_stress
            + self.weight_mem * mem_stress
            + self.weight_hpa_gap * hpa_gap_stress
        )

        # EWMA trend detection
        if not self._initialised:
            self._ewma_latency = p95_safe
            self._ewma_rps = snap.rps
            self._initialised = True
        else:
            self._ewma_latency = self.alpha * p95_safe + (1 - self.alpha) * self._ewma_latency
            self._ewma_rps = self.alpha * snap.rps + (1 - self.alpha) * self._ewma_rps

        # Trend boost: rising latency above EWMA → additional stress
        if self._ewma_latency > 0 and p95_safe > self._ewma_latency * 1.2:
            trend_boost = min((p95_safe - self._ewma_latency) / self._ewma_latency, 0.2)
            score += trend_boost

        return _clamp(score)

    def _normalise_latency(self, p95_ms: float) -> float:
        """Map p95 latency (ms) → [0, 1].  0 ms → 0, ≥ ceiling → 1."""
        return _clamp(p95_ms / self.latency_ceiling_ms)

    def _normalise_error_rate(self, rate: float) -> float:
        """Non-linear boost: small errors are low stress, high errors ramp fast."""
        if rate <= 0:
            return 0.0
        return _clamp(math.sqrt(rate / self.error_rate_ceiling))

    def _normalise_pending_pods(self, count: int) -> float:
        """0 pending → 0, ≥ ceiling → 1."""
        return _clamp(count / self.pending_pods_ceiling)

    @staticmethod
    def _normalise_hpa_gap(desired: int, current: int) -> float:
        """Gap between desired and current replicas → [0, 1]."""
        if current <= 0 or desired <= current:
            return 0.0
        return _clamp((desired - current) / current)

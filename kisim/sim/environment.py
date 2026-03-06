"""
KISim Simulation Environment.

A Kubernetes-oriented rollout simulation environment inspired by the KIS-S
(Kubernetes Inference Simulator with RL-Based Auto-Scaling) framework
[Li et al., 2025, arXiv:2507.07932] and adapted for deploy-time strategy
selection under autoscaling dynamics.

While KIS-S targets GPU inference auto-scaling, this simulator was
substantially re-engineered to model rollout-specific concerns:
- Workload traffic patterns (steady, ramp, spike) derived from public traces
- Autoscaling (HPA) behaviour with cooldown and scheduling pressure
- Pod lifecycle (startup, warm-up, ready) during progressive delivery
- Service latency under load with overload cascading
- Fault injection effects (pod-kill, network-latency)
- Rollout strategy execution (delay, pre-scale, canary, rolling)

References:
    - KIS-S: https://github.com/GuilinDev/KISim (MIT License)
    - Li et al. (2025). "KIS-S: A GPU-Aware Kubernetes Inference Simulator
      with RL-Based Auto-Scaling." arXiv:2507.07932.
    - WorldCup98 traces: https://ita.ee.lbl.gov/html/contrib/WorldCup.html
    - Alibaba Cluster Trace v2018 for pressure regime calibration
"""

from dataclasses import dataclass, field
from enum import Enum

import numpy as np


class WorkloadPattern(Enum):
    STEADY = "steady"
    RAMP = "ramp"
    SPIKE = "spike"


class FaultType(Enum):
    NONE = "none"
    POD_KILL = "pod_kill"
    NETWORK_LATENCY = "network_latency"


class RolloutAction(Enum):
    DELAY = "delay"
    PRE_SCALE = "pre-scale"
    CANARY = "canary"
    ROLLING = "rolling"


@dataclass
class SimConfig:
    """Configuration for a simulation episode."""

    # Workload
    workload_pattern: WorkloadPattern = WorkloadPattern.STEADY
    base_rps: float = 100.0
    peak_rps: float = 500.0
    ramp_duration_steps: int = 20
    spike_start_step: int = 10
    spike_duration_steps: int = 5

    # Cluster
    initial_replicas: int = 2
    min_replicas: int = 2
    max_replicas: int = 20
    target_cpu_util: float = 0.6
    hpa_cooldown_steps: int = 3
    node_cpu_capacity: float = 1.0
    node_mem_capacity: float = 1.0
    background_cpu_load: float = 0.2
    background_mem_load: float = 0.3

    # Service
    base_latency_ms: float = 10.0
    latency_per_rps_per_replica: float = 0.05
    warmup_steps: int = 3
    warmup_latency_multiplier: float = 3.0
    error_rate_base: float = 0.001
    error_rate_overload_factor: float = 0.1

    # Capacity model
    max_rps_per_replica: float = 50.0

    # Fault injection
    fault_type: FaultType = FaultType.NONE
    fault_start_step: int = 15
    fault_duration_steps: int = 5
    fault_pods_affected: int = 1
    fault_latency_add_ms: float = 200.0

    # Episode
    total_steps: int = 50
    rollout_start_step: int = 5

    # Rollout parameters
    new_replicas_target: int = 4
    canary_steps_weight: list[float] = field(default_factory=lambda: [0.1, 0.25, 0.5, 1.0])
    canary_pause_steps: int = 3
    delay_steps: int = 5
    prescale_extra: int = 2

    # SLO thresholds
    slo_p95_latency_ms: float = 100.0
    slo_error_rate: float = 0.01

    # Cost weights
    w_slo_violation: float = 10.0
    w_latency_impact: float = 5.0
    w_resource_overhead: float = 5.0
    w_duration: float = 4.0
    w_failure_penalty: float = 20.0

    # Deployment disruption parameters
    rolling_disruption_peak: float = 0.4  # fraction of capacity lost during rolling
    canary_disruption_factor: float = 0.3  # disruption per unit of weight change

    # Failure detection
    overload_failure_threshold: float = 1.4  # load factor triggering cascading failure
    overload_failure_steps: int = 3  # consecutive overload steps before rollback

    # Scheduling pressure (affects pre-scale)
    scheduling_pressure_cpu_threshold: float = 0.75

    # Stress forecast (external signal)
    # -1 = compute from ground-truth workload pattern (default for simulator)
    #  0 = no forecast available (default for production / ablation)
    # >0 = forecast stress level from external service
    stress_forecast: float = -1.0

    # Random seed
    seed: int | None = None


@dataclass
class StepState:
    """State at a single simulation time step."""

    step: int
    rps: float
    current_replicas: int
    desired_replicas: int
    ready_replicas: int
    pending_pods: int
    p95_latency_ms: float
    p99_latency_ms: float
    error_rate: float
    node_cpu_util: float
    node_mem_util: float
    restart_count: int
    canary_weight: float
    rollout_progress: float
    fault_active: bool


@dataclass
class EpisodeResult:
    """Result of a complete simulation episode."""

    config: SimConfig
    action: RolloutAction
    states: list[StepState]

    # Decision snapshot (at rollout start)
    decision_snapshot: dict[str, float]
    stress_score: float

    # Outcome metrics
    slo_violation_steps: int
    slo_violation_seconds: float
    p95_peak_ms: float
    p99_peak_ms: float
    error_rate_peak: float
    total_replica_seconds: float
    success: bool
    rollback: bool
    duration_steps: int
    computed_cost: float


class KISimEnvironment:
    """
    Kubernetes-Inspired Simulation Environment.

    Simulates a single rollout episode and returns observation features
    matching the online Decision Snapshot schema.
    """

    def __init__(self, config: SimConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed)
        self.reset()

    def reset(self):
        """Reset environment to initial state."""
        self.step = 0
        self.current_replicas = self.config.initial_replicas
        self.desired_replicas = self.config.initial_replicas
        self.ready_replicas = self.config.initial_replicas
        self.pending_pods = 0
        self.warming_up_pods = 0
        self.restart_count = 0
        self.canary_weight = 0.0
        self.rollout_started = False
        self.rollout_complete = False
        self.rollout_failed = False
        self.hpa_cooldown = 0
        self.consecutive_overload = 0
        self.states: list[StepState] = []

    def get_traffic(self, step: int) -> float:
        """Generate traffic load for the given step."""
        cfg = self.config

        if cfg.workload_pattern == WorkloadPattern.STEADY:
            rps = cfg.base_rps
        elif cfg.workload_pattern == WorkloadPattern.RAMP:
            progress = min(step / max(cfg.ramp_duration_steps, 1), 1.0)
            rps = cfg.base_rps + (cfg.peak_rps - cfg.base_rps) * progress
        elif cfg.workload_pattern == WorkloadPattern.SPIKE:
            if cfg.spike_start_step <= step < cfg.spike_start_step + cfg.spike_duration_steps:
                rps = cfg.peak_rps
            else:
                rps = cfg.base_rps
        else:
            rps = cfg.base_rps

        # Add noise
        noise = self.rng.normal(0, rps * 0.05)
        return max(rps + noise, 0)

    def simulate_hpa(self, rps: float):
        """Simulate HPA autoscaling decisions."""
        cfg = self.config

        if self.hpa_cooldown > 0:
            self.hpa_cooldown -= 1
            return

        # Estimate CPU utilisation from RPS
        rps_per_replica = rps / max(self.current_replicas, 1)
        estimated_cpu = rps_per_replica / cfg.max_rps_per_replica

        if estimated_cpu > cfg.target_cpu_util:
            # Scale up
            new_desired = min(
                int(np.ceil(rps / (cfg.max_rps_per_replica * cfg.target_cpu_util))),
                cfg.max_replicas,
            )
            if new_desired > self.desired_replicas:
                self.desired_replicas = new_desired
                self.hpa_cooldown = cfg.hpa_cooldown_steps
        elif estimated_cpu < cfg.target_cpu_util * 0.5 and self.current_replicas > cfg.min_replicas:
            # Scale down
            new_desired = max(
                int(np.ceil(rps / (cfg.max_rps_per_replica * cfg.target_cpu_util))),
                cfg.min_replicas,
            )
            if new_desired < self.desired_replicas:
                self.desired_replicas = new_desired
                self.hpa_cooldown = cfg.hpa_cooldown_steps

    def simulate_pod_lifecycle(self):
        """Simulate pod creation, warmup, and readiness."""
        # Pending → warming up
        if self.desired_replicas > self.current_replicas:
            new_pods = self.desired_replicas - self.current_replicas
            self.pending_pods = new_pods
            self.warming_up_pods += new_pods
            self.current_replicas = self.desired_replicas

        # Warming up → ready
        if self.warming_up_pods > 0:
            ready_this_step = max(1, self.warming_up_pods // self.config.warmup_steps)
            self.warming_up_pods = max(0, self.warming_up_pods - ready_this_step)
            self.ready_replicas = self.current_replicas - self.warming_up_pods
            self.pending_pods = max(0, self.pending_pods - ready_this_step)
        else:
            self.ready_replicas = self.current_replicas
            self.pending_pods = 0

    def compute_deployment_disruption(
        self, step: int, action: RolloutAction, rollout_actual_start: int, canary_schedule: list
    ) -> float:
        """
        Compute the fraction of capacity disrupted by pod transitions.

        Rolling updates cause large simultaneous disruption (pods restart
        together under maxUnavailable). Canary/delay/pre-scale shift traffic
        gradually, causing smaller disruptions at each weight change.
        """
        cfg = self.config

        if step < rollout_actual_start:
            return 0.0

        if action in (RolloutAction.ROLLING, RolloutAction.DELAY):
            # All pods transition simultaneously -- large capacity dip
            # Batched replacement: disruption lasts warmup_steps x 3
            steps_since_start = step - rollout_actual_start
            rolling_window = cfg.warmup_steps * 3
            if steps_since_start < rolling_window:
                progress = steps_since_start / max(rolling_window, 1)
                return cfg.rolling_disruption_peak * (1.0 - progress)
            return 0.0

        # CANARY, PRE_SCALE -- gradual traffic shift
        disruption = 0.0
        for i, (step_t, weight) in enumerate(canary_schedule):
            if step >= step_t and step < step_t + cfg.warmup_steps:
                weight_change = weight if i == 0 else weight - canary_schedule[i - 1][1]
                steps_since_change = step - step_t
                progress = steps_since_change / max(cfg.warmup_steps, 1)
                step_disruption = (
                    abs(weight_change) * cfg.canary_disruption_factor * (1.0 - progress)
                )
                disruption = max(disruption, step_disruption)
        return disruption

    def compute_latency(
        self, rps: float, fault_active: bool, override_replicas: float | None = None
    ) -> tuple[float, float]:
        """Compute p95 and p99 latency based on load and capacity."""
        cfg = self.config
        effective_replicas = max(
            override_replicas if override_replicas is not None else float(self.ready_replicas), 1.0
        )

        rps_per_replica = rps / effective_replicas
        load_factor = rps_per_replica / cfg.max_rps_per_replica

        # Base latency + load-dependent component
        latency = cfg.base_latency_ms + cfg.latency_per_rps_per_replica * rps_per_replica

        # Overload penalty (exponential above capacity)
        if load_factor > 0.8:
            overload = (load_factor - 0.8) / 0.2
            latency *= 1 + overload**2 * 5

        # Warm-up penalty
        if self.warming_up_pods > 0:
            warmup_fraction = self.warming_up_pods / max(self.current_replicas, 1)
            latency *= 1 + warmup_fraction * (cfg.warmup_latency_multiplier - 1)

        # Fault penalty
        if fault_active and cfg.fault_type == FaultType.NETWORK_LATENCY:
            fraction_affected = cfg.fault_pods_affected / max(effective_replicas, 1)
            latency += cfg.fault_latency_add_ms * fraction_affected

        # Add noise
        p95 = latency * (1 + self.rng.exponential(0.1))
        p99 = p95 * (1 + self.rng.exponential(0.15))

        return max(p95, 1.0), max(p99, 1.0)

    def compute_error_rate(
        self, rps: float, fault_active: bool, override_replicas: float | None = None
    ) -> float:
        """Compute error rate based on load and faults."""
        cfg = self.config
        effective_replicas = max(
            override_replicas if override_replicas is not None else float(self.ready_replicas), 1.0
        )
        load_factor = rps / (effective_replicas * cfg.max_rps_per_replica)

        error_rate = cfg.error_rate_base

        # Overload errors
        if load_factor > 1.0:
            error_rate += (load_factor - 1.0) * cfg.error_rate_overload_factor

        # Fault errors
        if fault_active and cfg.fault_type == FaultType.POD_KILL:
            killed_fraction = cfg.fault_pods_affected / max(self.current_replicas, 1)
            error_rate += killed_fraction * 0.3
            self.restart_count += cfg.fault_pods_affected

        return min(error_rate, 1.0)

    def compute_node_utilisation(self, rps: float) -> tuple[float, float]:
        """Compute approximate node CPU and memory utilisation."""
        cfg = self.config
        cpu = cfg.background_cpu_load + (self.current_replicas * 0.03) + (rps * 0.0005)
        mem = cfg.background_mem_load + (self.current_replicas * 0.02)
        return min(cpu, 1.0), min(mem, 1.0)

    def compute_stress_forecast(self) -> float:
        """Compute stress forecast signal from ground-truth workload pattern.

        Returns a value in [0, 1] indicating the expected traffic
        increase during the deployment window relative to current traffic.

        In the testbed the experiment harness knows the k6 script and can
        provide this as ground truth.  In production, an external
        workload-forecasting service would supply this value (or 0.0 if
        no forecast is available).
        """
        if self.config.stress_forecast >= 0:
            # Externally provided forecast - use as-is
            return float(np.clip(self.config.stress_forecast, 0.0, 1.0))

        # Ground-truth derivation from known workload pattern
        t_now = self.config.rollout_start_step
        window = 15  # look-ahead window (steps)
        t_end = min(t_now + window, self.config.total_steps)

        current_rps = max(self.get_traffic(t_now), 1.0)
        future_peak = max(self.get_traffic(t) for t in range(t_now, t_end + 1))

        # Ratio of expected peak to current, normalised so 3x = 1.0
        increase_ratio = (future_peak / current_rps) - 1.0
        return float(np.clip(increase_ratio / 3.0, 0.0, 1.0))

    def get_decision_snapshot(self, rps: float) -> dict[str, float]:
        """Build a Decision Snapshot matching the online schema."""
        p95, p99 = self.compute_latency(rps, False)
        error_rate = self.compute_error_rate(rps, False)
        cpu, mem = self.compute_node_utilisation(rps)

        # Compute RPS trend (slope over lookback window)
        lookback = 5
        start_step = max(0, self.config.rollout_start_step - lookback)
        rps_samples = [
            self.get_traffic(s) for s in range(start_step, self.config.rollout_start_step + 1)
        ]
        if len(rps_samples) >= 2:
            rps_trend = (rps_samples[-1] - rps_samples[0]) / max(self.config.base_rps, 1.0)
        else:
            rps_trend = 0.0

        # Stress forecast signal
        stress_forecast = self.compute_stress_forecast()

        return {
            "rps": rps,
            "p95_latency_ms": p95,
            "p99_latency_ms": p99,
            "error_rate": error_rate,
            "pending_pods": float(self.pending_pods),
            "restart_count": float(self.restart_count),
            "node_cpu_util": cpu,
            "node_mem_util": mem,
            "hpa_desired_replicas": float(self.desired_replicas),
            "hpa_current_replicas": float(self.current_replicas),
            "target_replicas": float(self.config.new_replicas_target),
            "warmup_class_encoded": 1.0 if self.config.warmup_steps > 3 else 0.0,
            "rps_trend": rps_trend,
            "stress_forecast": stress_forecast,
        }

    def compute_stress_score(self, snapshot: dict[str, float]) -> float:
        """Compute StressScore matching the online controller logic.

        This computes a composite stress metric from current system
        observables (latency, errors, resource utilisation, HPA gap).
        The ``stress_forecast`` signal is NOT folded in here; instead
        it is provided as a **separate state dimension** so the agent
        can learn distinct policies for calm-vs-spike-expected contexts.
        """
        w_lat, w_err, w_pending, w_cpu, w_mem, w_hpa = 0.25, 0.25, 0.15, 0.15, 0.10, 0.10

        lat_stress = min(snapshot["p95_latency_ms"] / 500.0, 1.0)
        err_stress = (
            min(np.sqrt(snapshot["error_rate"] / 0.05), 1.0) if snapshot["error_rate"] > 0 else 0
        )
        pending_stress = min(snapshot["pending_pods"] / 10.0, 1.0)
        cpu_stress = np.clip(snapshot["node_cpu_util"], 0, 1)
        mem_stress = np.clip(snapshot["node_mem_util"], 0, 1)

        hpa_current = max(snapshot["hpa_current_replicas"], 1)
        hpa_gap = max(snapshot["hpa_desired_replicas"] - hpa_current, 0) / hpa_current
        hpa_stress = min(hpa_gap, 1.0)

        current_score = (
            w_lat * lat_stress
            + w_err * err_stress
            + w_pending * pending_stress
            + w_cpu * cpu_stress
            + w_mem * mem_stress
            + w_hpa * hpa_stress
        )

        return float(np.clip(current_score, 0, 1))

    def run_episode(self, action: RolloutAction) -> EpisodeResult:
        """
        Run a complete simulation episode with the given action.

        Models strategy-differentiated deployment dynamics:
        - Rolling: fast but causes large capacity dip (pod replacement)
        - Canary: slow but safe (gradual traffic shift)
        - Delay: waits for stress to pass before deploying
        - Pre-scale: adds headroom before gradual deployment

        Includes failure detection: sustained overload → rollback.
        """
        self.reset()
        cfg = self.config

        # Collect decision snapshot at rollout start step
        pre_rps = self.get_traffic(cfg.rollout_start_step)
        decision_snapshot = self.get_decision_snapshot(pre_rps)
        stress_score = self.compute_stress_score(decision_snapshot)

        # Apply action-specific modifications
        rollout_actual_start = cfg.rollout_start_step
        if action == RolloutAction.DELAY:
            rollout_actual_start += cfg.delay_steps
        elif action == RolloutAction.PRE_SCALE:
            extra = cfg.prescale_extra
            # Under high node pressure, scheduling is slower/partial
            if cfg.background_cpu_load > cfg.scheduling_pressure_cpu_threshold:
                pressure_ratio = (
                    cfg.background_cpu_load - cfg.scheduling_pressure_cpu_threshold
                ) / (1.0 - cfg.scheduling_pressure_cpu_threshold)
                # Some pods may fail to schedule under extreme pressure
                schedulable = max(1, int(extra * (1.0 - pressure_ratio * 0.7)))
                extra = schedulable
            self.desired_replicas = min(self.current_replicas + extra, cfg.max_replicas)

        # Canary weight schedule
        if action in (RolloutAction.ROLLING, RolloutAction.DELAY):
            # Rolling / Delay: instant transition (all traffic to new version)
            canary_schedule = [(rollout_actual_start, 1.0)]
        elif action in (RolloutAction.CANARY, RolloutAction.PRE_SCALE):
            canary_schedule = []
            step_offset = rollout_actual_start
            for w in cfg.canary_steps_weight:
                canary_schedule.append((step_offset, w))
                step_offset += cfg.canary_pause_steps
        else:
            canary_schedule = [(rollout_actual_start, 1.0)]

        # Track metrics
        slo_violations = 0
        p95_peak = 0.0
        p99_peak = 0.0
        error_peak = 0.0
        total_replica_seconds = 0.0
        rollout_complete_step = cfg.total_steps
        final_step = cfg.total_steps

        # Simulate each time step
        for t in range(cfg.total_steps):
            rps = self.get_traffic(t)

            # HPA
            self.simulate_hpa(rps)
            self.simulate_pod_lifecycle()

            # Fault injection
            fault_active = (
                cfg.fault_type != FaultType.NONE
                and cfg.fault_start_step <= t < cfg.fault_start_step + cfg.fault_duration_steps
            )

            if fault_active and cfg.fault_type == FaultType.POD_KILL:
                killed = min(cfg.fault_pods_affected, self.ready_replicas - 1)
                if killed > 0:
                    self.ready_replicas -= killed
                    self.warming_up_pods += killed

            # Canary weight
            for step_t, weight in canary_schedule:
                if t >= step_t:
                    self.canary_weight = weight

            # ---- Deployment disruption model ----
            disruption = self.compute_deployment_disruption(
                t, action, rollout_actual_start, canary_schedule
            )
            effective_ready = max(1.0, self.ready_replicas * (1.0 - disruption))

            # Compute metrics using disrupted capacity
            p95, p99 = self.compute_latency(rps, fault_active, effective_ready)
            error_rate = self.compute_error_rate(rps, fault_active, effective_ready)
            cpu, mem = self.compute_node_utilisation(rps)

            # ---- Failure detection (cascading overload → rollback) ----
            load_factor = rps / (effective_ready * cfg.max_rps_per_replica)
            if t >= rollout_actual_start and load_factor > cfg.overload_failure_threshold:
                self.consecutive_overload += 1
                if self.consecutive_overload >= cfg.overload_failure_steps:
                    self.rollout_failed = True
            else:
                self.consecutive_overload = 0

            # Track rollout progress
            progress = self.canary_weight if t >= rollout_actual_start else 0.0
            if self.canary_weight >= 1.0 and not self.rollout_complete:
                self.rollout_complete = True
                rollout_complete_step = t

            state = StepState(
                step=t,
                rps=rps,
                current_replicas=self.current_replicas,
                desired_replicas=self.desired_replicas,
                ready_replicas=self.ready_replicas,
                pending_pods=self.pending_pods,
                p95_latency_ms=p95,
                p99_latency_ms=p99,
                error_rate=error_rate,
                node_cpu_util=cpu,
                node_mem_util=mem,
                restart_count=self.restart_count,
                canary_weight=self.canary_weight,
                rollout_progress=progress,
                fault_active=fault_active,
            )
            self.states.append(state)

            # SLO checks
            if p95 > cfg.slo_p95_latency_ms or error_rate > cfg.slo_error_rate:
                slo_violations += 1

            p95_peak = max(p95_peak, p95)
            p99_peak = max(p99_peak, p99)
            error_peak = max(error_peak, error_rate)
            total_replica_seconds += self.current_replicas

            # If failed, remaining steps count as SLO violations and episode ends
            if self.rollout_failed:
                remaining = cfg.total_steps - t - 1
                slo_violations += remaining
                final_step = t + 1
                break

        # ---- Compute cost with normalised components ----
        # Duration: from deployment intent to completion (includes delay wait)
        duration = max(rollout_complete_step - cfg.rollout_start_step, 0)
        duration_ratio = duration / max(cfg.total_steps, 1)

        # SLO violation: fraction of episode steps with SLO breach
        slo_ratio = slo_violations / max(final_step, 1)

        # Latency impact: how far above SLO (capped at 10x)
        latency_norm = np.clip((p95_peak / cfg.slo_p95_latency_ms - 1.0), 0, 10) / 10.0

        # Resource overhead: extra replica-seconds beyond baseline
        baseline_replica_seconds = cfg.initial_replicas * cfg.total_steps
        overhead = max(total_replica_seconds - baseline_replica_seconds, 0)
        resource_norm = np.clip(overhead / max(baseline_replica_seconds, 1), 0, 5) / 5.0

        # Failure indicator
        failure = 1.0 if self.rollout_failed else 0.0

        cost = (
            cfg.w_slo_violation * slo_ratio
            + cfg.w_latency_impact * latency_norm
            + cfg.w_resource_overhead * resource_norm
            + cfg.w_duration * duration_ratio
            + cfg.w_failure_penalty * failure
        )

        return EpisodeResult(
            config=cfg,
            action=action,
            states=self.states,
            decision_snapshot=decision_snapshot,
            stress_score=stress_score,
            slo_violation_steps=slo_violations,
            slo_violation_seconds=slo_violations * 1.0,
            p95_peak_ms=p95_peak,
            p99_peak_ms=p99_peak,
            error_rate_peak=error_peak,
            total_replica_seconds=total_replica_seconds,
            success=self.rollout_complete and not self.rollout_failed,
            rollback=self.rollout_failed,
            duration_steps=duration,
            computed_cost=cost,
        )

"""
Scenario generator for KISim.

Produces a grid of simulation configurations covering the experiment space:
- Workload patterns: steady, ramp, spike (with varying magnitudes)
- Trace-inspired profiles: WorldCup98 flash crowd, Azure Functions multi-tenancy
- Fault types: none, pod_kill, network_latency
- Cluster pressure: low, medium, high (calibrated from Alibaba Cluster Trace v2018)
- Rollout sizes: small, large

Traffic intensity profiles are informed by public datasets:
    - WorldCup98 (Arlitt & Jin, 2000): flash-crowd 4-6x bursts in <60s
    - Azure Functions (Shahrad et al., 2020): multi-tenant co-firing bursts
    - Alibaba Cluster Trace v2018: CPU utilisation distributions for pressure bands
"""

from kisim.sim.environment import FaultType, SimConfig, WorkloadPattern

# Workload pattern configs
# The first 8 are synthetic; the last 4 are trace-calibrated
WORKLOAD_CONFIGS = {
    # --- Synthetic patterns ---
    "steady_low": {"pattern": WorkloadPattern.STEADY, "base_rps": 50, "peak_rps": 50},
    "steady_med": {"pattern": WorkloadPattern.STEADY, "base_rps": 200, "peak_rps": 200},
    "ramp_moderate": {"pattern": WorkloadPattern.RAMP, "base_rps": 50, "peak_rps": 300},
    "ramp_heavy": {"pattern": WorkloadPattern.RAMP, "base_rps": 100, "peak_rps": 600},
    "ramp_steep": {
        "pattern": WorkloadPattern.RAMP,
        "base_rps": 80,
        "peak_rps": 800,
        "ramp_duration": 10,
    },
    "spike_short": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 100,
        "peak_rps": 500,
        "spike_start": 10,
        "spike_duration": 5,
    },
    "spike_long": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 100,
        "peak_rps": 800,
        "spike_start": 10,
        "spike_duration": 10,
    },
    "spike_overlap": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 100,
        "peak_rps": 600,
        "spike_start": 3,
        "spike_duration": 12,
    },
    # --- Trace-calibrated patterns ---
    # WorldCup98 flash crowd: 5x surge (Arlitt & Jin, 2000)
    "wc98_flash": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 80,
        "peak_rps": 400,
        "spike_start": 8,
        "spike_duration": 8,
    },
    # WorldCup98 sustained peak (match-day plateau)
    "wc98_sustained": {
        "pattern": WorkloadPattern.RAMP,
        "base_rps": 80,
        "peak_rps": 350,
        "ramp_duration": 15,
    },
    # Azure Functions co-firing burst (Shahrad et al., 2020)
    "azfunc_burst": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 20,
        "peak_rps": 120,
        "spike_start": 5,
        "spike_duration": 6,
    },
    # Azure Functions cron overlap (batch + scheduled triggers)
    "azfunc_cron": {
        "pattern": WorkloadPattern.SPIKE,
        "base_rps": 30,
        "peak_rps": 180,
        "spike_start": 4,
        "spike_duration": 10,
    },
}

# Fault configs
FAULT_CONFIGS = {
    "none": {"type": FaultType.NONE},
    "pod_kill": {"type": FaultType.POD_KILL, "pods": 1},
    "network_latency": {"type": FaultType.NETWORK_LATENCY, "latency_ms": 200},
}

# Cluster pressure configs
# Calibrated from Alibaba Cluster Trace v2018 CPU utilisation percentiles:
#   low  ≈ p25 (~15% CPU, ~20% mem)
#   medium ≈ p50-p75 (~45% CPU, ~50% mem)
#   high ≈ p90+ (~70% CPU, ~75% mem)
PRESSURE_CONFIGS = {
    "low": {"bg_cpu": 0.15, "bg_mem": 0.2},
    "medium": {"bg_cpu": 0.45, "bg_mem": 0.5},
    "high": {"bg_cpu": 0.7, "bg_mem": 0.75},
}

# Rollout size configs
ROLLOUT_CONFIGS = {
    "small": {"target_replicas": 3, "warmup_steps": 2},
    "large": {"target_replicas": 8, "warmup_steps": 5},
}


def generate_scenario_grid(seed_base: int = 42) -> list[dict]:
    """Generate the full scenario grid for experiments."""
    scenarios = []
    scenario_id = 0

    for wl_name, wl_cfg in WORKLOAD_CONFIGS.items():
        for fault_name, fault_cfg in FAULT_CONFIGS.items():
            for pressure_name, pressure_cfg in PRESSURE_CONFIGS.items():
                for rollout_name, rollout_cfg in ROLLOUT_CONFIGS.items():
                    scenario = {
                        "id": f"s{scenario_id:04d}",
                        "workload": wl_name,
                        "fault": fault_name,
                        "pressure": pressure_name,
                        "rollout_size": rollout_name,
                        "config": build_config(
                            wl_cfg,
                            fault_cfg,
                            pressure_cfg,
                            rollout_cfg,
                            seed=seed_base + scenario_id,
                        ),
                    }
                    scenarios.append(scenario)
                    scenario_id += 1

    return scenarios


def build_config(wl, fault, pressure, rollout, seed=42) -> SimConfig:
    """Build a SimConfig from component configs."""
    cfg = SimConfig(seed=seed)

    # Workload
    cfg.workload_pattern = wl["pattern"]
    cfg.base_rps = wl["base_rps"]
    cfg.peak_rps = wl["peak_rps"]
    if "spike_duration" in wl:
        cfg.spike_duration_steps = wl["spike_duration"]
    if "spike_start" in wl:
        cfg.spike_start_step = wl["spike_start"]
    if "ramp_duration" in wl:
        cfg.ramp_duration_steps = wl["ramp_duration"]

    # Fault
    cfg.fault_type = fault["type"]
    if "pods" in fault:
        cfg.fault_pods_affected = fault["pods"]
    if "latency_ms" in fault:
        cfg.fault_latency_add_ms = fault["latency_ms"]

    # Pressure
    cfg.background_cpu_load = pressure["bg_cpu"]
    cfg.background_mem_load = pressure["bg_mem"]

    # Rollout size
    cfg.new_replicas_target = rollout["target_replicas"]
    cfg.warmup_steps = rollout["warmup_steps"]

    return cfg


def generate_training_configs(num_episodes: int = 50000, seed: int = 42) -> list[SimConfig]:
    """Generate randomised training configurations for Q-learning."""
    rng = __import__("numpy").random.default_rng(seed)
    configs = []

    for _i in range(num_episodes):
        cfg = SimConfig(seed=int(rng.integers(0, 2**31)))

        # Randomise workload pattern
        pattern = rng.choice(list(WorkloadPattern))
        cfg.workload_pattern = pattern
        cfg.base_rps = rng.uniform(20, 300)
        cfg.peak_rps = cfg.base_rps * rng.uniform(1.5, 5.0)

        # Randomise timing so stress overlaps with deployment
        cfg.rollout_start_step = int(rng.integers(3, 10))
        if pattern == WorkloadPattern.SPIKE:
            # Spike sometimes before, during, or after rollout
            cfg.spike_start_step = int(rng.integers(0, 15))
            cfg.spike_duration_steps = int(rng.integers(3, 15))
        if pattern == WorkloadPattern.RAMP:
            cfg.ramp_duration_steps = int(rng.integers(8, 30))

        # Randomise faults (timing overlaps with deployment)
        fault = rng.choice(list(FaultType), p=[0.5, 0.25, 0.25])
        cfg.fault_type = fault
        if fault == FaultType.POD_KILL:
            cfg.fault_pods_affected = int(rng.integers(1, 3))
            cfg.fault_start_step = int(
                rng.integers(max(0, cfg.rollout_start_step - 3), cfg.rollout_start_step + 10)
            )
        elif fault == FaultType.NETWORK_LATENCY:
            cfg.fault_latency_add_ms = rng.uniform(50, 500)
            cfg.fault_start_step = int(
                rng.integers(max(0, cfg.rollout_start_step - 3), cfg.rollout_start_step + 10)
            )

        # Randomise cluster pressure (wider range for diversity)
        cfg.background_cpu_load = rng.uniform(0.1, 0.9)
        cfg.background_mem_load = rng.uniform(0.1, 0.85)

        # Randomise rollout
        cfg.new_replicas_target = int(rng.integers(2, 10))
        cfg.warmup_steps = int(rng.integers(1, 8))
        cfg.initial_replicas = int(rng.integers(1, 5))

        configs.append(cfg)

    return configs

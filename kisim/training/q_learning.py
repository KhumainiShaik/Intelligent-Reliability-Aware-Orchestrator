"""
Tabular Q-Learning Training Pipeline.

Trains a policy offline using KISim episodes.
- Single-step episodic MDP: state → action → reward
- State = discretised Decision Snapshot
- Action ∈ {delay, pre-scale, canary, rolling}
- Reward = -Cost
"""

import json
import logging
import os
import time
from collections import defaultdict

import numpy as np
from tqdm import tqdm

from kisim.sim.environment import (
    KISimEnvironment,
    RolloutAction,
)
from kisim.sim.scenarios import generate_training_configs

logger = logging.getLogger(__name__)


# Feature specification for discretisation
# Reduced bins for better Q-table coverage.
# Compact default state: stress_score captures current health and cpu_util
# captures resource headroom. Forecasting is intentionally optional; the live
# controller may still collect stress_forecast, but a forecasting service is
# not required for the default deployable policy.
# Total default state space: 5x2 = 10 states × 4 actions = 40 state-action pairs.
FEATURE_SPEC = [
    {"name": "stress_score", "min_val": 0.0, "max_val": 1.0, "num_bins": 5},
    {"name": "cpu_util", "min_val": 0.0, "max_val": 1.0, "num_bins": 2},
]

ACTIONS = [a.value for a in RolloutAction]
ACTION_MAP = {a.value: a for a in RolloutAction}

# All possible feature look-up values (superset across versions)
_ALL_FEATURE_VALUES = {
    "stress_score": lambda snap, ss: ss,
    "rps": lambda snap, ss: snap.get("rps", 0),
    "p95_latency": lambda snap, ss: snap.get("p95_latency_ms", 0),
    "error_rate": lambda snap, ss: snap.get("error_rate", 0),
    "pending_pods": lambda snap, ss: snap.get("pending_pods", 0),
    "cpu_util": lambda snap, ss: snap.get("node_cpu_util", 0),
    "hpa_gap": lambda snap, ss: max(
        snap.get("hpa_desired_replicas", 0) - snap.get("hpa_current_replicas", 0), 0
    ),
    "rps_trend": lambda snap, ss: snap.get("rps_trend", 0),
    "stress_forecast": lambda snap, ss: snap.get("stress_forecast", 0),
}


def compute_bins(spec: dict) -> list[float]:
    """Compute bin edges for a feature."""
    return np.linspace(spec["min_val"], spec["max_val"], spec["num_bins"]).tolist()


def discretise_snapshot(
    snapshot: dict[str, float],
    stress_score: float,
    bins: dict[str, list[float]],
    feature_spec: list[dict] | None = None,
) -> str:
    """Discretise a decision snapshot into a state key.

    If *feature_spec* is provided (e.g. from a loaded policy artifact)
    it is used instead of the module-level FEATURE_SPEC.  This lets
    policies trained with different feature sets be evaluated correctly.
    """
    spec_list = feature_spec if feature_spec is not None else FEATURE_SPEC

    parts = []
    for spec in spec_list:
        fn = _ALL_FEATURE_VALUES.get(spec["name"])
        val = fn(snapshot, stress_score) if fn else 0
        bin_edges = bins[spec["name"]]
        idx = int(np.digitize(val, bin_edges))
        parts.append(str(idx))

    return "_".join(parts)


class QLearningTrainer:
    """Tabular Q-learning trainer for rollout strategy selection."""

    def __init__(
        self,
        alpha: float = 0.1,  # Learning rate
        gamma: float = 0.0,  # Discount (0 for single-step episodic)
        epsilon: float = 0.3,  # ε-greedy exploration
        epsilon_decay: float = 0.9998,
        epsilon_min: float = 0.02,
        seed: int = 42,
    ):
        self.alpha = alpha
        self.gamma = gamma
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min = epsilon_min
        self.rng = np.random.default_rng(seed)

        # Q-table: state_key → [Q-values per action]
        self.q_table: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(ACTIONS))

        # Visit counts per (state, action) for sample-mean update
        self.sa_visit_counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(ACTIONS))

        # Bins for discretisation
        self.bins = {spec["name"]: compute_bins(spec) for spec in FEATURE_SPEC}

        # Training statistics
        self.episode_costs: list[float] = []
        self.episode_actions: list[str] = []
        self.state_visit_counts: dict[str, int] = defaultdict(int)

    def select_action(self, state_key: str) -> tuple[int, str]:
        """ε-greedy action selection."""
        if self.rng.random() < self.epsilon:
            action_idx = int(self.rng.integers(0, len(ACTIONS)))
        else:
            q_values = self.q_table[state_key]
            action_idx = int(np.argmax(q_values))

        return action_idx, ACTIONS[action_idx]

    def update(self, state_key: str, action_idx: int, reward: float):
        """Sample-mean update (exact Monte Carlo for single-step MDP).

        Uses Q(s,a) = Q(s,a) + (1/n)(r - Q(s,a)) which converges to
        the true expected reward, unlike exponential averaging.
        """
        self.sa_visit_counts[state_key][action_idx] += 1
        n = self.sa_visit_counts[state_key][action_idx]
        alpha = 1.0 / n  # Exact sample mean
        old_q = self.q_table[state_key][action_idx]
        self.q_table[state_key][action_idx] = old_q + alpha * (reward - old_q)

    def decay_epsilon(self):
        """Decay exploration rate."""
        self.epsilon = max(self.epsilon * self.epsilon_decay, self.epsilon_min)

    def train(self, num_episodes: int = 50000, seed: int = 42) -> dict:
        """Run the full training loop."""
        logger.info("Generating %d training configurations...", num_episodes)
        configs = generate_training_configs(num_episodes, seed)

        logger.info("Training Q-learning for %d episodes...", num_episodes)
        start_time = time.time()

        for _i, cfg in enumerate(tqdm(configs, desc="Training")):
            env = KISimEnvironment(cfg)

            # Get decision snapshot at rollout start
            rps = env.get_traffic(cfg.rollout_start_step)
            for t in range(cfg.rollout_start_step):
                traffic = env.get_traffic(t)
                env.simulate_hpa(traffic)
                env.simulate_pod_lifecycle()

            snapshot = env.get_decision_snapshot(rps)
            stress_score = env.compute_stress_score(snapshot)
            state_key = discretise_snapshot(snapshot, stress_score, self.bins)

            # Select action
            action_idx, action_name = self.select_action(state_key)
            action = ACTION_MAP[action_name]

            # Run episode
            env.reset()
            result = env.run_episode(action)

            # Compute reward = -cost
            reward = -result.computed_cost

            # Update Q-table
            self.update(state_key, action_idx, reward)
            self.decay_epsilon()

            # Track statistics
            self.episode_costs.append(result.computed_cost)
            self.episode_actions.append(action_name)
            self.state_visit_counts[state_key] += 1

        elapsed = time.time() - start_time
        logger.info("Training complete in %.1fs", elapsed)
        logger.info("Unique states visited: %d", len(self.q_table))
        logger.info("Final epsilon: %.4f", self.epsilon)

        return self.get_training_summary()

    def get_training_summary(self) -> dict:
        """Return training statistics."""
        costs = np.array(self.episode_costs)
        window = min(1000, len(costs))
        return {
            "total_episodes": len(self.episode_costs),
            "unique_states": len(self.q_table),
            "mean_cost": float(np.mean(costs)),
            "final_mean_cost": float(np.mean(costs[-window:])),
            "std_cost": float(np.std(costs)),
            "final_epsilon": self.epsilon,
            "action_distribution": {
                a: self.episode_actions.count(a) / len(self.episode_actions) for a in ACTIONS
            },
        }

    def export_artifact(self, output_dir: str, version: str = "v1") -> str:
        """Export policy artifact (Q-table + bins + feature spec)."""
        os.makedirs(output_dir, exist_ok=True)

        artifact = {
            "version": version,
            "features": FEATURE_SPEC,
            "bins": self.bins,
            "q_table": dict(self.q_table),
            "actions": ACTIONS,
            "training_summary": self.get_training_summary(),
        }

        path = os.path.join(output_dir, "policy_artifact.json")
        with open(path, "w") as f:
            json.dump(artifact, f, indent=2, default=str)

        logger.info("Policy artifact exported to %s", path)
        return path

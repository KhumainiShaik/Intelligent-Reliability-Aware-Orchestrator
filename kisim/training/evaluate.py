"""
Evaluate a trained policy against baselines.

Usage:
    python -m training.evaluate --policy artifacts/v1/policy_artifact.json
"""

import argparse
import json
import logging
from collections import defaultdict

import numpy as np
from tqdm import tqdm

from kisim.sim.environment import KISimEnvironment
from kisim.sim.scenarios import generate_scenario_grid
from kisim.training.q_learning import (
    ACTION_MAP,
    ACTIONS,
    discretise_snapshot,
)

logger = logging.getLogger(__name__)


def load_policy(artifact_path: str) -> dict:
    """Load a policy artifact."""
    with open(artifact_path) as f:
        return json.load(f)


def policy_select(artifact: dict, snapshot: dict, stress_score: float) -> str:
    """Select action using loaded policy artifact."""
    bins = artifact["bins"]
    feature_spec = artifact.get("features")  # Use artifact's feature spec
    state_key = discretise_snapshot(snapshot, stress_score, bins, feature_spec=feature_spec)
    q_values = artifact["q_table"].get(state_key)

    if q_values is None:
        # Fallback: rule-based
        return rule_based_select(snapshot, stress_score)

    return artifact["actions"][int(np.argmax(q_values))]


def rule_based_select(snapshot: dict, stress_score: float) -> str:
    """Rule-based policy for comparison."""
    stress_forecast = snapshot.get("stress_forecast", 0)

    # If forecast says stress is coming, pre-scale proactively
    if stress_forecast > 0.3 and stress_score > 0.3:
        return "pre-scale"

    if stress_score > 0.7:
        return "pre-scale"
    elif stress_score > 0.5:
        return "canary"
    elif stress_score > 0.3:
        hpa_gap = snapshot.get("hpa_desired_replicas", 0) - snapshot.get("hpa_current_replicas", 0)
        if hpa_gap > 2:
            return "pre-scale"
        return "canary"
    return "rolling"


def evaluate_policy(policy_fn, scenarios: list[dict], name: str, repeats: int = 3) -> dict:
    """Evaluate a policy across the scenario grid."""
    results = defaultdict(list)

    for scenario in tqdm(scenarios, desc=f"Evaluating {name}"):
        for repeat in range(repeats):
            cfg = scenario["config"]
            cfg.seed = cfg.seed + repeat * 1000  # Vary seed per repeat

            env = KISimEnvironment(cfg)

            # Get decision snapshot
            rps = env.get_traffic(cfg.rollout_start_step)
            snapshot = env.get_decision_snapshot(rps)
            stress_score = env.compute_stress_score(snapshot)

            # Select action
            action_name = policy_fn(snapshot, stress_score)
            action = ACTION_MAP[action_name]

            # Run episode
            result = env.run_episode(action)

            results[scenario["id"]].append(
                {
                    "scenario_id": scenario["id"],
                    "workload": scenario["workload"],
                    "fault": scenario["fault"],
                    "pressure": scenario["pressure"],
                    "rollout_size": scenario["rollout_size"],
                    "action": action_name,
                    "cost": result.computed_cost,
                    "slo_violations": result.slo_violation_steps,
                    "p95_peak": result.p95_peak_ms,
                    "error_peak": result.error_rate_peak,
                    "replica_seconds": result.total_replica_seconds,
                    "success": result.success,
                    "duration": result.duration_steps,
                }
            )

    return dict(results)


def compute_statistics(results: dict) -> dict:
    """Compute summary statistics with confidence intervals."""
    all_costs = []
    all_slo = []
    all_success = []

    for _scenario_id, trials in results.items():
        for trial in trials:
            all_costs.append(trial["cost"])
            all_slo.append(trial["slo_violations"])
            all_success.append(1 if trial["success"] else 0)

    costs = np.array(all_costs)
    slo = np.array(all_slo)
    success = np.array(all_success)

    # Bootstrap confidence intervals
    n_boot = 10000
    boot_means = []
    for _ in range(n_boot):
        idx = np.random.randint(0, len(costs), size=len(costs))
        boot_means.append(np.mean(costs[idx]))
    boot_means = np.array(boot_means)

    return {
        "mean_cost": float(np.mean(costs)),
        "std_cost": float(np.std(costs)),
        "ci_95_lower": float(np.percentile(boot_means, 2.5)),
        "ci_95_upper": float(np.percentile(boot_means, 97.5)),
        "mean_slo_violations": float(np.mean(slo)),
        "success_rate": float(np.mean(success)),
        "total_trials": len(all_costs),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate trained RL policy")
    parser.add_argument(
        "--policy",
        type=str,
        default="artifacts/v1/policy_artifact.json",
        help="Path to policy artifact",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Repeats per scenario")
    parser.add_argument("--output", type=str, default="evaluation_results.json", help="Output file")
    args = parser.parse_args()

    scenarios = generate_scenario_grid()
    logger.info("Scenario grid: %d scenarios x %d repeats", len(scenarios), args.repeats)

    # Evaluate RL policy
    artifact = load_policy(args.policy)

    def rl_fn(snap, ss):
        return policy_select(artifact, snap, ss)

    rl_results = evaluate_policy(rl_fn, scenarios, "RL Policy", args.repeats)
    rl_stats = compute_statistics(rl_results)

    # Evaluate rule-based policy
    rb_results = evaluate_policy(rule_based_select, scenarios, "Rule-Based", args.repeats)
    rb_stats = compute_statistics(rb_results)

    # Evaluate fixed baselines
    fixed_results = {}
    for action_name in ACTIONS:

        def fixed_fn(snap, ss, a=action_name):
            return a

        results = evaluate_policy(fixed_fn, scenarios, f"Fixed-{action_name}", args.repeats)
        fixed_results[action_name] = compute_statistics(results)

    # Report
    logger.info("\n" + "=" * 60)
    logger.info("EVALUATION RESULTS")
    logger.info("=" * 60)
    logger.info(
        "\n%-20s %-12s %-24s %-12s %-10s", "Policy", "Mean Cost", "95% CI", "SLO Viol.", "Success"
    )
    logger.info("-" * 80)

    logger.info(
        "%-20s %-12.4f [%.4f, %.4f]  %-12.2f %.2f%%",
        "RL Policy",
        rl_stats["mean_cost"],
        rl_stats["ci_95_lower"],
        rl_stats["ci_95_upper"],
        rl_stats["mean_slo_violations"],
        rl_stats["success_rate"] * 100,
    )

    logger.info(
        "%-20s %-12.4f [%.4f, %.4f]  %-12.2f %.2f%%",
        "Rule-Based",
        rb_stats["mean_cost"],
        rb_stats["ci_95_lower"],
        rb_stats["ci_95_upper"],
        rb_stats["mean_slo_violations"],
        rb_stats["success_rate"] * 100,
    )

    for action_name, stats in fixed_results.items():
        logger.info(
            "%-20s %-12.4f [%.4f, %.4f]  %-12.2f %.2f%%",
            "Fixed-" + action_name,
            stats["mean_cost"],
            stats["ci_95_lower"],
            stats["ci_95_upper"],
            stats["mean_slo_violations"],
            stats["success_rate"] * 100,
        )

    # Save results
    output = {
        "rl_policy": {"stats": rl_stats, "policy_version": artifact.get("version")},
        "rule_based": {"stats": rb_stats},
        "fixed_baselines": {name: {"stats": s} for name, s in fixed_results.items()},
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    logger.info("Results saved to %s", args.output)


if __name__ == "__main__":
    main()

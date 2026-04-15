"""
Evaluation and Statistical Analysis Pipeline.

Computes:
- Bootstrap confidence intervals
- Mann-Whitney U tests for policy comparisons
- Ablation analysis (feature/action removal)
- Result visualisations and summary tables
"""

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


def load_episode_logs(episode_dir: str) -> pd.DataFrame:
    """Load all episode JSON records into a DataFrame."""
    records = []
    for path in Path(episode_dir).glob("episode_*.json"):
        with open(path) as f:
            rec = json.load(f)
        # Flatten nested fields
        flat = {
            "run_id": rec.get("run_id"),
            "timestamp": rec.get("timestamp"),
            "policy_version": rec.get("policy_version"),
            "stress_score": rec.get("stress_score"),
            "chosen_action": rec.get("chosen_action"),
            "original_action": rec.get("original_action"),
            "guardrail_override": rec.get("guardrail_override"),
        }
        # Snapshot fields
        snap = rec.get("decision_snapshot", {})
        for k, v in snap.items():
            flat[f"snap_{k}"] = v
        # Outcome fields
        outcome = rec.get("outcome", {})
        if outcome:
            for k, v in outcome.items():
                flat[f"outcome_{k}"] = v
        records.append(flat)

    return pd.DataFrame(records)


def bootstrap_ci(
    data: np.ndarray, stat_fn=np.mean, n_boot: int = 10000, ci: float = 0.95, seed: int = 42
) -> tuple[float, float, float]:
    """Compute bootstrap confidence interval for a statistic."""
    rng = np.random.default_rng(seed)
    boot_stats = []
    for _ in range(n_boot):
        sample = rng.choice(data, size=len(data), replace=True)
        boot_stats.append(stat_fn(sample))
    boot_stats = np.array(boot_stats)

    alpha = (1 - ci) / 2
    lower = np.percentile(boot_stats, alpha * 100)
    upper = np.percentile(boot_stats, (1 - alpha) * 100)
    point = stat_fn(data)

    return point, lower, upper


def mann_whitney_test(group_a: np.ndarray, group_b: np.ndarray) -> dict:
    """Perform Mann-Whitney U test between two groups."""
    if len(group_a) < 1 or len(group_b) < 1:
        return {
            "U_statistic": 0.0,
            "p_value": 1.0,
            "effect_size": 0.0,
            "significant_005": False,
            "significant_001": False,
            "n_a": len(group_a),
            "n_b": len(group_b),
        }

    statistic, p_value = stats.mannwhitneyu(group_a, group_b, alternative="two-sided")

    # Effect size (rank-biserial correlation)
    n1, n2 = len(group_a), len(group_b)
    effect_size = 1 - (2 * statistic) / (n1 * n2)

    return {
        "U_statistic": float(statistic),
        "p_value": float(p_value),
        "effect_size": float(effect_size),
        "significant_005": p_value < 0.05,
        "significant_001": p_value < 0.01,
        "n_a": n1,
        "n_b": n2,
    }


def compare_policies(df: pd.DataFrame, metric: str = "outcome_computed_cost") -> dict:
    """Compare all policies pairwise using Mann-Whitney U tests."""
    policies = df["policy_version"].unique()
    comparisons = {}

    for i, p1 in enumerate(policies):
        for p2 in policies[i + 1 :]:
            data_a = df[df["policy_version"] == p1][metric].dropna().values
            data_b = df[df["policy_version"] == p2][metric].dropna().values

            if len(data_a) < 3 or len(data_b) < 3:
                continue

            test = mann_whitney_test(data_a, data_b)
            key = f"{p1} vs {p2}"
            comparisons[key] = test
            comparisons[key]["mean_a"] = float(np.mean(data_a))
            comparisons[key]["mean_b"] = float(np.mean(data_b))

    return comparisons


def compute_policy_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Compute summary statistics per policy."""
    metrics = [
        "outcome_computed_cost",
        "outcome_slo_violation_seconds",
        "outcome_p95_impact",
        "outcome_replica_seconds",
        "outcome_duration_seconds",
    ]

    rows = []
    for policy in df["policy_version"].unique():
        policy_df = df[df["policy_version"] == policy]
        row = {"policy": policy, "n_episodes": len(policy_df)}

        for metric in metrics:
            if metric in policy_df.columns:
                data = policy_df[metric].dropna().values
                if len(data) > 0:
                    point, ci_lo, ci_hi = bootstrap_ci(data)
                    row[f"{metric}_mean"] = point
                    row[f"{metric}_ci_lo"] = ci_lo
                    row[f"{metric}_ci_hi"] = ci_hi

        # Success rate
        if "outcome_success" in policy_df.columns:
            success = policy_df["outcome_success"].dropna().values
            row["success_rate"] = float(np.mean(success))

        # Action distribution
        action_counts = policy_df["chosen_action"].value_counts(normalize=True)
        for action in ["delay", "pre-scale", "canary", "rolling"]:
            row[f"action_pct_{action}"] = float(action_counts.get(action, 0))

        rows.append(row)

    return pd.DataFrame(rows)


def ablation_analysis(df: pd.DataFrame) -> dict:
    """Perform ablation analysis: what happens when features/actions are removed."""
    results = {}

    # Action ablation: compare with restricted action sets
    for removed_action in ["delay", "pre-scale", "canary", "rolling"]:
        with_action = df[df["chosen_action"] != removed_action]
        without_metric = "outcome_computed_cost"
        if without_metric in df.columns:
            full_costs = df[without_metric].dropna().values
            ablated_costs = with_action[without_metric].dropna().values
            if len(full_costs) > 3 and len(ablated_costs) > 3:
                test = mann_whitney_test(full_costs, ablated_costs)
                results[f"remove_{removed_action}"] = test

    return results


def generate_report(episode_dir: str, output_dir: str):
    """Generate full evaluation report."""
    os.makedirs(output_dir, exist_ok=True)

    logger.info("Loading episode logs...")
    df = load_episode_logs(episode_dir)
    logger.info("Loaded %d episodes", len(df))

    if len(df) == 0:
        logger.warning("No episodes found. Exiting.")
        return

    # Policy summary
    logger.info("Computing policy summaries...")
    summary_df = compute_policy_summary(df)
    summary_df.to_csv(os.path.join(output_dir, "policy_summary.csv"), index=False)
    logger.info("Policy summary:\n%s", summary_df.to_string())

    # Pairwise comparisons
    logger.info("Pairwise comparisons (Mann-Whitney U)...")
    comparisons = compare_policies(df)
    with open(os.path.join(output_dir, "pairwise_comparisons.json"), "w") as f:
        json.dump(comparisons, f, indent=2)

    for pair, result in comparisons.items():
        sig = "***" if result["significant_001"] else ("*" if result["significant_005"] else "ns")
        logger.info(
            "  %s: p=%.4f %s (mean_a=%.4f, mean_b=%.4f)",
            pair,
            result["p_value"],
            sig,
            result["mean_a"],
            result["mean_b"],
        )

    # Ablation analysis
    logger.info("Ablation analysis...")
    ablation = ablation_analysis(df)
    with open(os.path.join(output_dir, "ablation_results.json"), "w") as f:
        json.dump(ablation, f, indent=2)

    for removed, result in ablation.items():
        sig = "***" if result["significant_001"] else ("*" if result["significant_005"] else "ns")
        logger.info("  %s: p=%.4f %s", removed, result["p_value"], sig)

    # Scenario-level breakdown
    logger.info("Scenario breakdown...")
    if "snap_rps" in df.columns and "outcome_computed_cost" in df.columns:
        scenario_summary = df.groupby(["chosen_action"]).agg(
            {
                "outcome_computed_cost": ["mean", "std", "count"],
                "stress_score": "mean",
            }
        )
        scenario_summary.to_csv(os.path.join(output_dir, "scenario_breakdown.csv"))
        logger.info("Scenario breakdown:\n%s", scenario_summary.to_string())

    logger.info("Report saved to %s/", output_dir)


if __name__ == "__main__":
    episode_dir = sys.argv[1] if len(sys.argv) > 1 else "experiments/episodes"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "experiments/reports"
    generate_report(episode_dir, output_dir)

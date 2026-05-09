"""
Visualisation module for evaluation results.

Generates publication-quality plots for conference papers.
"""

import logging
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Publication style
plt.rcParams.update(
    {
        "font.size": 11,
        "font.family": "serif",
        "axes.labelsize": 12,
        "axes.titlesize": 13,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
        "figure.figsize": (8, 5),
        "figure.dpi": 300,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
    }
)

COLORS = {
    "rl": "#2196F3",
    "rule-based": "#FF9800",
    "delay": "#4CAF50",
    "pre-scale": "#9C27B0",
    "canary": "#F44336",
    "rolling": "#607D8B",
}


def plot_cost_comparison(summary_df: pd.DataFrame, output_path: str):
    """Bar chart comparing mean cost with confidence intervals across policies."""
    _fig, ax = plt.subplots(figsize=(10, 6))

    policies = summary_df["policy"].values
    means = summary_df["outcome_computed_cost_mean"].values
    ci_lo = summary_df["outcome_computed_cost_ci_lo"].values
    ci_hi = summary_df["outcome_computed_cost_ci_hi"].values
    errors = np.array([means - ci_lo, ci_hi - means])

    ax.bar(
        range(len(policies)),
        means,
        yerr=errors,
        capsize=5,
        color=[COLORS.get(p.split("-")[0] if "-" in p else p, "#999") for p in policies],
        edgecolor="black",
        linewidth=0.5,
        alpha=0.85,
    )

    ax.set_xticks(range(len(policies)))
    ax.set_xticklabels(policies, rotation=30, ha="right")
    ax.set_ylabel("Mean Cost")
    ax.set_title("Policy Comparison: Mean Rollout Cost (95% CI)")
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info("Saved: %s", output_path)


def plot_stress_vs_action(df: pd.DataFrame, output_path: str):
    """Scatter plot of stress score vs chosen action, coloured by cost."""
    _fig, ax = plt.subplots(figsize=(10, 6))

    for action in df["chosen_action"].unique():
        subset = df[df["chosen_action"] == action]
        if "outcome_computed_cost" in subset.columns:
            y = subset["outcome_computed_cost"]
        else:
            y = np.zeros(len(subset))
        ax.scatter(
            subset["stress_score"],
            y,
            label=action,
            alpha=0.5,
            s=20,
            color=COLORS.get(action, "#999"),
        )

    ax.set_xlabel("Stress Score")
    ax.set_ylabel("Episode Cost")
    ax.set_title("Action Selection vs Stress Score")
    ax.legend()
    ax.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info("Saved: %s", output_path)


def plot_slo_violations(df: pd.DataFrame, output_path: str):
    """Box plot of SLO violations per policy."""
    if "outcome_slo_violation_seconds" not in df.columns:
        return

    _fig, ax = plt.subplots(figsize=(10, 6))

    policies = df["policy_version"].unique()
    data = [
        df[df["policy_version"] == p]["outcome_slo_violation_seconds"].dropna().values
        for p in policies
    ]

    try:
        bp = ax.boxplot(data, tick_labels=policies, patch_artist=True)
    except TypeError:
        bp = ax.boxplot(data, labels=policies, patch_artist=True)
    for patch, policy in zip(bp["boxes"], policies, strict=False):
        patch.set_facecolor(COLORS.get(policy.split("-")[0] if "-" in policy else policy, "#ccc"))
        patch.set_alpha(0.7)

    ax.set_ylabel("SLO Violation Time (seconds)")
    ax.set_title("SLO Violations by Policy")
    ax.set_xticklabels(policies, rotation=30, ha="right")
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info("Saved: %s", output_path)


def plot_action_distribution(df: pd.DataFrame, output_path: str):
    """Stacked bar chart showing action distribution per policy."""
    actions = ["delay", "pre-scale", "canary", "rolling"]
    policies = df["policy_version"].unique()

    _fig, ax = plt.subplots(figsize=(10, 6))

    bottoms = np.zeros(len(policies))
    for action in actions:
        counts = []
        for policy in policies:
            policy_df = df[df["policy_version"] == policy]
            count = (policy_df["chosen_action"] == action).sum() / len(policy_df)
            counts.append(count)
        ax.bar(
            range(len(policies)),
            counts,
            bottom=bottoms,
            label=action,
            color=COLORS.get(action, "#999"),
            alpha=0.85,
        )
        bottoms += counts

    ax.set_xticks(range(len(policies)))
    ax.set_xticklabels(policies, rotation=30, ha="right")
    ax.set_ylabel("Action Proportion")
    ax.set_title("Action Distribution by Policy")
    ax.legend()
    ax.set_ylim(0, 1.05)
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info("Saved: %s", output_path)


def plot_training_curve(costs: list[float], output_path: str, window: int = 500):
    """Plot training cost curve with moving average."""
    _fig, ax = plt.subplots(figsize=(10, 5))

    costs_arr = np.array(costs)
    ax.plot(costs_arr, alpha=0.1, color="gray", linewidth=0.5)

    # Moving average
    if len(costs_arr) > window:
        ma = pd.Series(costs_arr).rolling(window=window, min_periods=1).mean()
        ax.plot(ma.values, color=COLORS["rl"], linewidth=1.5, label=f"Moving avg (w={window})")

    ax.set_xlabel("Episode")
    ax.set_ylabel("Cost")
    ax.set_title("Q-Learning Training Curve")
    ax.legend()
    ax.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info("Saved: %s", output_path)


def generate_all_plots(episode_dir: str, output_dir: str):
    """Generate all evaluation plots."""
    from evaluation.analyse import compute_policy_summary, load_episode_logs

    os.makedirs(output_dir, exist_ok=True)
    df = load_episode_logs(episode_dir)

    if len(df) == 0:
        logger.warning("No data to plot.")
        return

    summary_df = compute_policy_summary(df)

    if "outcome_computed_cost_mean" in summary_df.columns:
        plot_cost_comparison(summary_df, os.path.join(output_dir, "cost_comparison.png"))

    plot_stress_vs_action(df, os.path.join(output_dir, "stress_vs_action.png"))
    plot_slo_violations(df, os.path.join(output_dir, "slo_violations.png"))
    plot_action_distribution(df, os.path.join(output_dir, "action_distribution.png"))


if __name__ == "__main__":
    import sys

    episode_dir = sys.argv[1] if len(sys.argv) > 1 else "experiments/episodes"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "experiments/plots"
    generate_all_plots(episode_dir, output_dir)

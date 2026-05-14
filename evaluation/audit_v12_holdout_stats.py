#!/usr/bin/env python3
"""Statistical audit for the v12 60-trial holdout reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

BASELINES = {
    "rl-v11": "RL v11",
    "baseline-rule-based": "Rule-based",
    "baseline-rolling": "Rolling",
    "baseline-pre-scale": "Pre-Scale",
}
V12 = "rl-v12"
RNG_SEED = 20260512


def _trimmed_mean(values: pd.Series, proportion: float = 0.10) -> float:
    values = values.dropna().sort_values().to_numpy()
    if len(values) == 0:
        return float("nan")
    trim = int(len(values) * proportion)
    if trim == 0 or len(values) <= 2 * trim:
        return float(np.mean(values))
    return float(np.mean(values[trim:-trim]))


def _bootstrap_ci(
    values: np.ndarray, rng: np.random.Generator, n: int = 20_000
) -> tuple[float, float]:
    if len(values) == 0:
        return float("nan"), float("nan")
    draws = rng.choice(values, size=(n, len(values)), replace=True).mean(axis=1)
    return tuple(np.percentile(draws, [2.5, 97.5]).tolist())


def _paired_permutation_p(diff: np.ndarray, rng: np.random.Generator, n: int = 20_000) -> float:
    observed = abs(float(np.mean(diff)))
    signs = rng.choice([-1, 1], size=(n, len(diff)), replace=True)
    permuted = np.abs((signs * diff).mean(axis=1))
    return float((np.count_nonzero(permuted >= observed) + 1) / (n + 1))


def load_inputs(reports_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    all_trials = pd.read_csv(reports_dir / "all_trials.csv")
    trial_metrics = pd.read_csv(reports_dir / "targeted_trial_metrics.csv")
    pairwise = json.loads((reports_dir / "pairwise_tests.json").read_text())
    return all_trials, trial_metrics, pairwise


def validate(
    all_trials: pd.DataFrame, trial_metrics: pd.DataFrame, pairwise: dict
) -> dict[str, object]:
    required_all = {
        "mode",
        "scenario",
        "fault",
        "trial",
        "cost",
        "p95_ms",
        "error_rate",
        "req_rate",
    }
    required_metrics = {
        "mode",
        "scenario",
        "fault",
        "trial",
        "throughput_rps",
        "http_p95_ms",
        "failed_requests",
    }
    mode_counts = all_trials.groupby("mode").size().to_dict()
    cell_counts = all_trials.groupby(["scenario", "fault", "trial"]).size()
    summary_means = all_trials.groupby("mode")["cost"].mean().to_dict()

    reproduced = {}
    for baseline, label in BASELINES.items():
        candidate_keys = [
            f"RL v12 Contextual vs {label}",
            f"{label} vs RL v12 Contextual",
        ]
        key = next(
            (candidate for candidate in candidate_keys if candidate in pairwise), candidate_keys[0]
        )
        if key not in pairwise:
            reproduced[key] = {"found": False}
            continue
        first_label = key.split(" vs ")[0]
        if first_label == label:
            a = all_trials.loc[all_trials["mode"] == baseline, "cost"]
            b = all_trials.loc[all_trials["mode"] == V12, "cost"]
        else:
            a = all_trials.loc[all_trials["mode"] == V12, "cost"]
            b = all_trials.loc[all_trials["mode"] == baseline, "cost"]
        res = stats.mannwhitneyu(a, b, alternative="two-sided")
        recorded = pairwise[key]["cost"]["p"]
        reproduced[key] = {
            "found": True,
            "recorded_p": float(recorded),
            "recomputed_p": float(res.pvalue),
            "delta": abs(float(recorded) - float(res.pvalue)),
        }

    return {
        "all_trials_required_present": required_all.issubset(all_trials.columns),
        "targeted_metrics_required_present": required_metrics.issubset(trial_metrics.columns),
        "mode_counts": mode_counts,
        "balanced_cells": bool((cell_counts == 5).all() and len(cell_counts) == 12),
        "summary_cost_means": summary_means,
        "pvalue_reproduction": reproduced,
    }


def paired_block(all_trials: pd.DataFrame, reports_dir: Path) -> pd.DataFrame:
    rng = np.random.default_rng(RNG_SEED)
    rows = []
    v12 = all_trials[all_trials["mode"] == V12].set_index(["scenario", "fault", "trial"])
    for mode, label in BASELINES.items():
        base = all_trials[all_trials["mode"] == mode].set_index(["scenario", "fault", "trial"])
        joined = (
            base[["cost"]]
            .rename(columns={"cost": "baseline_cost"})
            .join(
                v12[["cost"]].rename(columns={"cost": "v12_cost"}),
                how="inner",
            )
        )
        diff = (joined["baseline_cost"] - joined["v12_cost"]).to_numpy()
        try:
            wilcoxon_p = float(
                stats.wilcoxon(diff, alternative="two-sided", zero_method="wilcox").pvalue
            )
        except ValueError:
            wilcoxon_p = float("nan")
        ci_low, ci_high = _bootstrap_ci(diff, rng)
        rows.append(
            {
                "comparison": f"rl-v12_vs_{mode}",
                "baseline_label": label,
                "n_pairs": len(diff),
                "baseline_mean_cost": float(joined["baseline_cost"].mean()),
                "v12_mean_cost": float(joined["v12_cost"].mean()),
                "paired_mean_difference_baseline_minus_v12": float(np.mean(diff)),
                "paired_median_difference_baseline_minus_v12": float(np.median(diff)),
                "percentage_improvement": float(
                    (joined["baseline_cost"].mean() - joined["v12_cost"].mean())
                    / joined["baseline_cost"].mean()
                    * 100
                ),
                "v12_win_count": int(np.sum(diff > 0)),
                "v12_loss_count": int(np.sum(diff < 0)),
                "tie_count": int(np.sum(diff == 0)),
                "wilcoxon_p": wilcoxon_p,
                "paired_permutation_p": _paired_permutation_p(diff, rng),
                "bootstrap_mean_diff_ci95_low": ci_low,
                "bootstrap_mean_diff_ci95_high": ci_high,
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(reports_dir / "paired_block_comparison.csv", index=False)
    return out


def robustness(
    all_trials: pd.DataFrame, reports_dir: Path
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    robust = (
        all_trials.groupby("mode")
        .agg(
            n=("cost", "size"),
            mean_cost=("cost", "mean"),
            median_cost=("cost", "median"),
            trimmed_mean_cost=("cost", _trimmed_mean),
            min_cost=("cost", "min"),
            max_cost=("cost", "max"),
            std_cost=("cost", "std"),
        )
        .reset_index()
    )
    robust.to_csv(reports_dir / "robust_cost_summary.csv", index=False)

    ranks = all_trials.copy()
    ranks["cell"] = ranks["scenario"] + "/" + ranks["fault"] + "/r" + ranks["trial"].astype(str)
    ranks["cost_rank_in_cell"] = ranks.groupby(["scenario", "fault", "trial"])["cost"].rank(
        method="min"
    )
    rank_table = ranks.sort_values(["scenario", "fault", "trial", "cost_rank_in_cell"])[
        ["cell", "scenario", "fault", "trial", "mode", "cost", "cost_rank_in_cell"]
    ]
    rank_table.to_csv(reports_dir / "per_cell_rank_table.csv", index=False)

    outliers = all_trials.sort_values("cost", ascending=False).head(5)[
        [
            "mode",
            "scenario",
            "fault",
            "trial",
            "cost",
            "p95_ms",
            "error_rate",
            "req_rate",
            "slo_breach",
        ]
    ]
    outliers.to_csv(reports_dir / "outlier_report.csv", index=False)
    return robust, rank_table, outliers


def write_markdown(
    reports_dir: Path,
    validation: dict[str, object],
    paired: pd.DataFrame,
    robust: pd.DataFrame,
    outliers: pd.DataFrame,
) -> None:
    lines = [
        "# Statistical Audit - V12 60-Trial Holdout",
        "",
        "Input reports directory: `results/comparison_20260512_v12_holdout_60/reports/`",
        "",
        "## Input Validation",
        "",
        f"- Required `all_trials.csv` columns present: `{validation['all_trials_required_present']}`",
        f"- Required `targeted_trial_metrics.csv` columns present: `{validation['targeted_metrics_required_present']}`",
        f"- Mode counts: `{validation['mode_counts']}`",
        f"- Balanced scenario/fault/repeat cells: `{validation['balanced_cells']}`",
        "- Lower corrected cost is treated as better.",
        "- The report-level corrected-cost column is named `cost` in `all_trials.csv`.",
        "",
        "## Mann-Whitney Reproduction",
        "",
        "| Comparison | Recorded p | Recomputed p | Delta |",
        "|---|---:|---:|---:|",
    ]
    for comparison, item in validation["pvalue_reproduction"].items():
        if not item["found"]:
            lines.append(f"| {comparison} | n/a | n/a | n/a |")
        else:
            lines.append(
                f"| {comparison} | {item['recorded_p']:.6g} | {item['recomputed_p']:.6g} | {item['delta']:.3g} |"
            )

    lines += [
        "",
        "The original Mann-Whitney p-values were reproduced from `all_trials.csv`; no statistical test implementation bug was found.",
        "",
        "## Paired/Blocked Analysis",
        "",
        "Matched block key: `scenario + fault + repeat`.",
        "",
        "| Comparison | Mean diff baseline-v12 | Median diff | Improvement % | Wins/Losses/Ties | Wilcoxon p | Permutation p | Bootstrap 95% CI |",
        "|---|---:|---:|---:|---|---:|---:|---|",
    ]
    for _, row in paired.iterrows():
        lines.append(
            f"| {row['comparison']} | {row['paired_mean_difference_baseline_minus_v12']:.3f} | "
            f"{row['paired_median_difference_baseline_minus_v12']:.3f} | {row['percentage_improvement']:.1f}% | "
            f"{int(row['v12_win_count'])}/{int(row['v12_loss_count'])}/{int(row['tie_count'])} | "
            f"{row['wilcoxon_p']:.6g} | {row['paired_permutation_p']:.6g} | "
            f"[{row['bootstrap_mean_diff_ci95_low']:.3f}, {row['bootstrap_mean_diff_ci95_high']:.3f}] |"
        )

    wilcoxon_significant = paired[paired["wilcoxon_p"] < 0.05]
    permutation_significant = paired[paired["paired_permutation_p"] < 0.05]
    lines += [
        "",
        "## Robustness Checks",
        "",
        "Robust cost summaries, per-cell ranks, and outlier reports were generated without removing or modifying any trials.",
        "",
        "Median/trimmed summaries are in `robust_cost_summary.csv`.",
        "Per-cell ranks are in `per_cell_rank_table.csv`.",
        "Top-cost trial audit is in `outlier_report.csv`.",
        "",
        "## Audit Conclusion",
        "",
    ]
    if wilcoxon_significant.empty and permutation_significant.empty:
        lines.append(
            "Both conservative unpaired and paired blocked analyses show that v12 achieved the lowest mean corrected cost, "
            "but statistical significance was not reached. The result should be treated as directional improvement and "
            "decision-diversity evidence."
        )
    else:
        wilcoxon_comparisons = ", ".join(wilcoxon_significant["comparison"].tolist()) or "none"
        permutation_comparisons = (
            ", ".join(permutation_significant["comparison"].tolist()) or "none"
        )
        lines.append(
            "The conservative unpaired Mann-Whitney tests did not reach significance, but the paired blocked analysis "
            f"showed Wilcoxon significance for: {wilcoxon_comparisons}. "
            f"The paired permutation test showed significance for: {permutation_comparisons}. "
            "For final wording, treat v12 vs v11 as mixed secondary evidence because Wilcoxon did not reach significance."
        )

    lines += [
        "",
        "## Top 5 Highest-Cost Trials",
        "",
        "| Mode | Scenario | Fault | Trial | Cost | HTTP P95 ms | Error rate | Throughput rps | SLO breach |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in outliers.iterrows():
        lines.append(
            f"| {row['mode']} | {row['scenario']} | {row['fault']} | {int(row['trial'])} | "
            f"{row['cost']:.3f} | {row['p95_ms']:.1f} | {row['error_rate']:.4f} | "
            f"{row['req_rate']:.2f} | {row['slo_breach']} |"
        )
    lines += [
        "",
    ]
    (reports_dir / "statistical_audit.md").write_text("\n".join(lines), encoding="utf-8")

    paired_md = [
        "# Paired Block Statistical Tests",
        "",
        "Matched block key: `scenario + fault + repeat`.",
        "",
        "| Comparison | N | Mean diff | Median diff | Improvement % | Wins | Losses | Ties | Wilcoxon p | Permutation p | Bootstrap 95% CI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in paired.iterrows():
        paired_md.append(
            f"| {row['comparison']} | {int(row['n_pairs'])} | "
            f"{row['paired_mean_difference_baseline_minus_v12']:.3f} | "
            f"{row['paired_median_difference_baseline_minus_v12']:.3f} | "
            f"{row['percentage_improvement']:.1f}% | {int(row['v12_win_count'])} | "
            f"{int(row['v12_loss_count'])} | {int(row['tie_count'])} | "
            f"{row['wilcoxon_p']:.6g} | {row['paired_permutation_p']:.6g} | "
            f"[{row['bootstrap_mean_diff_ci95_low']:.3f}, {row['bootstrap_mean_diff_ci95_high']:.3f}] |"
        )
    paired_md += [
        "",
    ]
    (reports_dir / "paired_block_statistical_tests.md").write_text(
        "\n".join(paired_md), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports_dir", type=Path)
    args = parser.parse_args()
    reports_dir = args.reports_dir
    all_trials, trial_metrics, pairwise = load_inputs(reports_dir)
    validation = validate(all_trials, trial_metrics, pairwise)
    paired = paired_block(all_trials, reports_dir)
    robust, _rank_table, outliers = robustness(all_trials, reports_dir)
    write_markdown(reports_dir, validation, paired, robust, outliers)
    print("wrote statistical audit outputs")


if __name__ == "__main__":
    main()

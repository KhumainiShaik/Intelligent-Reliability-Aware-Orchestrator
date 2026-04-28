#!/usr/bin/env python3
"""Five-mode comparison analysis for the Orchestrated Rollout evaluation.

Reads k6 trial summaries directly from shard directories, computes per-mode
statistics with bootstrap CIs, runs Mann-Whitney U pairwise tests, and
generates publication-quality plots.

Usage:
    python -m evaluation.compare_modes results/comparison_20260406_140509
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# SLO thresholds (from experiment_config.yaml)
SLO_P95_LATENCY_MS = 100.0
SLO_ERROR_RATE = 0.01

# Cost weights (from 02_component_behaviors_spec.md)
W_SLO_VIOLATION = 1.0
W_LATENCY_IMPACT = 0.5
W_RESOURCE_OVERHEAD = 0.2
W_FAILURE_PENALTY = 5.0

# Mode → correct shard suffix
MODE_SHARD_OF = {
    "rl": "-of-9",
    "baseline-rolling": "-of-5",
    "baseline-canary": "-of-4",
    "baseline-delay": "-of-5",
    "baseline-pre-scale": "-of-4",
}

# Display names for modes
MODE_LABELS = {
    "rl": "RL (v10b)",
    "baseline-rolling": "Rolling",
    "baseline-canary": "Canary",
    "baseline-delay": "Delay",
    "baseline-pre-scale": "Pre-Scale",
}

# Colours for plots
MODE_COLORS = {
    "rl": "#2196F3",
    "baseline-rolling": "#607D8B",
    "baseline-canary": "#F44336",
    "baseline-delay": "#4CAF50",
    "baseline-pre-scale": "#9C27B0",
}

plt.rcParams.update({
    "font.size": 11,
    "font.family": "serif",
    "axes.labelsize": 12,
    "axes.titlesize": 13,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.figsize": (10, 6),
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
})


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_k6_summary(path: Path) -> dict | None:
    """Extract key metrics from a k6 --summary-export JSON."""
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except Exception:
        logger.warning("Failed to parse %s", path)
        return None

    metrics = data.get("metrics") or {}

    # Duration
    duration_ms = (data.get("state") or {}).get("testRunDurationMs")
    duration_s = (_safe_float(duration_ms) or 0.0) / 1000.0
    if duration_s == 0.0:
        reqs = metrics.get("http_reqs") or {}
        reqs_v = reqs.get("values", reqs) if isinstance(reqs, dict) else {}
        rc = _safe_float(reqs_v.get("count"))
        rr = _safe_float(reqs_v.get("rate"))
        if rc and rr and rr > 0:
            duration_s = rc / rr

    # Latency: prefer inference_latency, fallback to http_req_duration
    for lat_key in ("inference_latency", "http_req_duration"):
        raw = metrics.get(lat_key) or {}
        vals = raw.get("values", raw) if isinstance(raw, dict) else {}
        p95 = _safe_float(vals.get("p(95)"))
        p50 = _safe_float(vals.get("med"))
        p99 = _safe_float(vals.get("p(99)"))
        avg = _safe_float(vals.get("avg"))
        if p95 is not None:
            break
    else:
        return None

    # Error rate
    hrf = metrics.get("http_req_failed") or {}
    hrf_v = hrf.get("values", hrf) if isinstance(hrf, dict) else {}
    error_rate = _safe_float(hrf_v.get("value")) or _safe_float(hrf_v.get("rate"))

    if error_rate is None:
        chk = metrics.get("checks") or {}
        chk_v = chk.get("values", chk) if isinstance(chk, dict) else {}
        pr = _safe_float(chk_v.get("value")) or _safe_float(chk_v.get("rate"))
        error_rate = (1.0 - pr) if pr is not None else 0.0

    # Request count / throughput
    reqs = metrics.get("http_reqs") or {}
    reqs_v = reqs.get("values", reqs) if isinstance(reqs, dict) else {}
    req_count = _safe_float(reqs_v.get("count")) or 0.0
    req_rate = _safe_float(reqs_v.get("rate")) or 0.0

    slo_breach = (p95 is not None and p95 > SLO_P95_LATENCY_MS) or (error_rate is not None and error_rate > SLO_ERROR_RATE)

    # Composite cost
    latency_norm = np.clip((p95 / SLO_P95_LATENCY_MS - 1.0), 0, 10) / 10.0 if p95 else 0.0
    cost = (
        W_SLO_VIOLATION * (1.0 if slo_breach else 0.0)
        + W_LATENCY_IMPACT * float(latency_norm)
    )

    return {
        "p95_ms": p95,
        "p50_ms": p50,
        "p99_ms": p99,
        "avg_ms": avg,
        "error_rate": error_rate,
        "duration_s": duration_s,
        "req_count": req_count,
        "req_rate": req_rate,
        "slo_breach": slo_breach,
        "cost": cost,
    }


def load_all_trials(experiments_dir: Path) -> pd.DataFrame:
    """Load k6 trial summaries from correct shard directories only."""
    rows = []
    for mode, shard_suffix in MODE_SHARD_OF.items():
        # Find top-level shard dirs for this mode
        shard_dirs = sorted(
            d for d in experiments_dir.iterdir()
            if d.is_dir()
            and f"_{mode}_shard" in d.name
            and d.name.endswith(shard_suffix.lstrip("-"))  # ends with e.g. "of-9"
        )
        # More robust: match pattern grid_*_{mode}_shard*{shard_suffix}
        shard_dirs = sorted(
            d for d in experiments_dir.iterdir()
            if d.is_dir() and f"_{mode}_shard" in d.name and shard_suffix in d.name
        )

        for shard_dir in shard_dirs:
            for combo_dir in sorted(shard_dir.iterdir()):
                if not combo_dir.is_dir():
                    continue
                combo_name = combo_dir.name
                parts = combo_name.split("_", 1)
                scenario = parts[0] if len(parts) >= 1 else combo_name
                fault = parts[1] if len(parts) >= 2 else "none"

                k6_dir = combo_dir / "k6_results"
                if not k6_dir.exists():
                    continue

                for summary_file in sorted(k6_dir.glob("trial_*_summary.json")):
                    trial_str = summary_file.stem.replace("trial_", "").replace("_summary", "")
                    try:
                        trial_num = int(trial_str)
                    except ValueError:
                        continue

                    parsed = parse_k6_summary(summary_file)
                    if parsed is None:
                        logger.warning("Skipping %s (parse failed)", summary_file)
                        continue

                    rows.append({
                        "mode": mode,
                        "scenario": scenario,
                        "fault": fault,
                        "combo": combo_name,
                        "trial": trial_num,
                        "shard": shard_dir.name,
                        **parsed,
                    })

    df = pd.DataFrame(rows)
    logger.info("Loaded %d trial records across %d modes", len(df), df["mode"].nunique())
    return df


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def bootstrap_ci(data, stat_fn=np.mean, n_boot=10_000, ci=0.95, seed=42):
    rng = np.random.default_rng(seed)
    arr = np.asarray(data, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return np.nan, np.nan, np.nan
    boots = np.array([stat_fn(rng.choice(arr, size=len(arr), replace=True)) for _ in range(n_boot)])
    alpha = (1 - ci) / 2
    return float(stat_fn(arr)), float(np.percentile(boots, alpha * 100)), float(np.percentile(boots, (1 - alpha) * 100))


def mann_whitney(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2:
        return {"U": np.nan, "p": 1.0, "r": 0.0, "sig": "n/a"}
    u, p = stats.mannwhitneyu(a, b, alternative="two-sided")
    r = 1 - 2 * u / (len(a) * len(b))  # rank-biserial
    sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else "ns"))
    return {"U": float(u), "p": float(p), "r": float(r), "sig": sig}


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(df: pd.DataFrame, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Per-mode summary ----
    summary_rows = []
    for mode in MODE_SHARD_OF:
        mdf = df[df["mode"] == mode]
        if len(mdf) == 0:
            continue
        row = {"mode": mode, "label": MODE_LABELS[mode], "n_trials": len(mdf)}
        for metric in ("p95_ms", "error_rate", "duration_s", "cost", "req_rate"):
            mean, ci_lo, ci_hi = bootstrap_ci(mdf[metric].values)
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci_lo"] = ci_lo
            row[f"{metric}_ci_hi"] = ci_hi
        row["slo_breach_pct"] = float(mdf["slo_breach"].mean()) * 100
        summary_rows.append(row)

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(output_dir / "mode_summary.csv", index=False)
    logger.info("Mode summary:\n%s", summary_df[["label", "n_trials", "p95_ms_mean", "error_rate_mean", "cost_mean", "slo_breach_pct"]].to_string(index=False))

    # ---- 2. Per-combo breakdown ----
    combo_rows = []
    for (mode, combo), gdf in df.groupby(["mode", "combo"]):
        row = {"mode": mode, "combo": combo, "n": len(gdf)}
        for metric in ("p95_ms", "error_rate", "cost"):
            row[f"{metric}_mean"] = gdf[metric].mean()
            row[f"{metric}_std"] = gdf[metric].std()
        row["slo_breach_pct"] = float(gdf["slo_breach"].mean()) * 100
        combo_rows.append(row)
    combo_df = pd.DataFrame(combo_rows)
    combo_df.to_csv(output_dir / "combo_breakdown.csv", index=False)

    # ---- 3. Pairwise Mann-Whitney U ----
    modes = list(MODE_SHARD_OF.keys())
    pairwise = {}
    for metric in ("p95_ms", "error_rate", "cost"):
        for i, m1 in enumerate(modes):
            for m2 in modes[i + 1:]:
                a = df[df["mode"] == m1][metric].dropna().values
                b = df[df["mode"] == m2][metric].dropna().values
                key = f"{MODE_LABELS[m1]} vs {MODE_LABELS[m2]}"
                if key not in pairwise:
                    pairwise[key] = {}
                test = mann_whitney(a, b)
                pairwise[key][metric] = {
                    "mean_a": float(np.nanmean(a)),
                    "mean_b": float(np.nanmean(b)),
                    **test,
                }

    with open(output_dir / "pairwise_tests.json", "w") as f:
        json.dump(pairwise, f, indent=2)

    # Print pairwise for cost
    logger.info("\nPairwise Mann-Whitney U (cost):")
    for pair, metrics_dict in pairwise.items():
        c = metrics_dict.get("cost", {})
        logger.info("  %s: mean=%.4f vs %.4f, p=%.4f %s (r=%.3f)",
                     pair, c.get("mean_a", 0), c.get("mean_b", 0),
                     c.get("p", 1), c.get("sig", ""), c.get("r", 0))

    # ---- 4. Per-scenario×fault heatmap data ----
    scenario_rows = []
    for (scenario, fault, mode), gdf in df.groupby(["scenario", "fault", "mode"]):
        scenario_rows.append({
            "scenario": scenario,
            "fault": fault,
            "mode": mode,
            "label": MODE_LABELS[mode],
            "p95_ms_mean": gdf["p95_ms"].mean(),
            "error_rate_mean": gdf["error_rate"].mean(),
            "cost_mean": gdf["cost"].mean(),
            "slo_breach_pct": float(gdf["slo_breach"].mean()) * 100,
        })
    scenario_df = pd.DataFrame(scenario_rows)
    scenario_df.to_csv(output_dir / "scenario_breakdown.csv", index=False)

    # ---- 5. Raw data ----
    df.to_csv(output_dir / "all_trials.csv", index=False)

    return summary_df, combo_df, scenario_df


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def generate_plots(df: pd.DataFrame, summary_df: pd.DataFrame, scenario_df: pd.DataFrame, output_dir: Path):
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(exist_ok=True)
    modes_ordered = [m for m in MODE_SHARD_OF if m in df["mode"].values]

    # ---- 1. Cost comparison bar chart ----
    fig, ax = plt.subplots(figsize=(10, 6))
    labels = [MODE_LABELS[m] for m in modes_ordered]
    means = [summary_df[summary_df["mode"] == m]["cost_mean"].values[0] for m in modes_ordered]
    ci_lo = [summary_df[summary_df["mode"] == m]["cost_ci_lo"].values[0] for m in modes_ordered]
    ci_hi = [summary_df[summary_df["mode"] == m]["cost_ci_hi"].values[0] for m in modes_ordered]
    errors = np.array([[m - lo for m, lo in zip(means, ci_lo)],
                       [hi - m for m, hi in zip(means, ci_hi)]])
    colors = [MODE_COLORS[m] for m in modes_ordered]
    bars = ax.bar(range(len(labels)), means, yerr=errors, capsize=5, color=colors,
                  edgecolor="black", linewidth=0.5, alpha=0.85)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Composite Cost")
    ax.set_title("Mean Rollout Cost by Mode (95% CI)")
    ax.grid(axis="y", alpha=0.3)
    for bar, val in zip(bars, means):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01, f"{val:.3f}",
                ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    fig.savefig(plots_dir / "cost_comparison.png")
    plt.close(fig)

    # ---- 2. P95 latency comparison ----
    fig, ax = plt.subplots(figsize=(10, 6))
    means_lat = [summary_df[summary_df["mode"] == m]["p95_ms_mean"].values[0] for m in modes_ordered]
    ci_lo_lat = [summary_df[summary_df["mode"] == m]["p95_ms_ci_lo"].values[0] for m in modes_ordered]
    ci_hi_lat = [summary_df[summary_df["mode"] == m]["p95_ms_ci_hi"].values[0] for m in modes_ordered]
    errors_lat = np.array([[m - lo for m, lo in zip(means_lat, ci_lo_lat)],
                           [hi - m for m, hi in zip(means_lat, ci_hi_lat)]])
    ax.bar(range(len(labels)), means_lat, yerr=errors_lat, capsize=5, color=colors,
           edgecolor="black", linewidth=0.5, alpha=0.85)
    ax.axhline(y=SLO_P95_LATENCY_MS, color="red", linestyle="--", linewidth=1, label=f"SLO = {SLO_P95_LATENCY_MS} ms")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("P95 Latency (ms)")
    ax.set_title("Mean P95 Latency by Mode (95% CI)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    fig.savefig(plots_dir / "p95_latency_comparison.png")
    plt.close(fig)

    # ---- 3. Error rate comparison ----
    fig, ax = plt.subplots(figsize=(10, 6))
    means_err = [summary_df[summary_df["mode"] == m]["error_rate_mean"].values[0] for m in modes_ordered]
    ci_lo_err = [summary_df[summary_df["mode"] == m]["error_rate_ci_lo"].values[0] for m in modes_ordered]
    ci_hi_err = [summary_df[summary_df["mode"] == m]["error_rate_ci_hi"].values[0] for m in modes_ordered]
    errors_err = np.array([[m - lo for m, lo in zip(means_err, ci_lo_err)],
                           [hi - m for m, hi in zip(means_err, ci_hi_err)]])
    ax.bar(range(len(labels)), means_err, yerr=errors_err, capsize=5, color=colors,
           edgecolor="black", linewidth=0.5, alpha=0.85)
    ax.axhline(y=SLO_ERROR_RATE, color="red", linestyle="--", linewidth=1, label=f"SLO = {SLO_ERROR_RATE}")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Error Rate")
    ax.set_title("Mean Error Rate by Mode (95% CI)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    fig.savefig(plots_dir / "error_rate_comparison.png")
    plt.close(fig)

    # ---- 4. SLO breach percentage ----
    fig, ax = plt.subplots(figsize=(10, 6))
    breach_pcts = [summary_df[summary_df["mode"] == m]["slo_breach_pct"].values[0] for m in modes_ordered]
    ax.bar(range(len(labels)), breach_pcts, color=colors, edgecolor="black", linewidth=0.5, alpha=0.85)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("SLO Breach Rate (%)")
    ax.set_title("Percentage of Trials with SLO Breach")
    ax.set_ylim(0, 105)
    ax.grid(axis="y", alpha=0.3)
    for i, pct in enumerate(breach_pcts):
        ax.text(i, pct + 1, f"{pct:.0f}%", ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    fig.savefig(plots_dir / "slo_breach_rate.png")
    plt.close(fig)

    # ---- 5. Box plot: p95 per mode ----
    fig, ax = plt.subplots(figsize=(10, 6))
    box_data = [df[df["mode"] == m]["p95_ms"].dropna().values for m in modes_ordered]
    bp = ax.boxplot(box_data, patch_artist=True, widths=0.6)
    for patch, m in zip(bp["boxes"], modes_ordered):
        patch.set_facecolor(MODE_COLORS[m])
        patch.set_alpha(0.7)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.axhline(y=SLO_P95_LATENCY_MS, color="red", linestyle="--", linewidth=1, label=f"SLO = {SLO_P95_LATENCY_MS} ms")
    ax.set_ylabel("P95 Latency (ms)")
    ax.set_title("P95 Latency Distribution by Mode")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    fig.savefig(plots_dir / "p95_boxplot.png")
    plt.close(fig)

    # ---- 6. Grouped bar: cost per scenario×fault ----
    combos = sorted(df["combo"].unique())
    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(combos))
    width = 0.15
    for i, m in enumerate(modes_ordered):
        vals = []
        for c in combos:
            subset = df[(df["mode"] == m) & (df["combo"] == c)]
            vals.append(subset["cost"].mean() if len(subset) > 0 else 0)
        ax.bar(x + i * width, vals, width, label=MODE_LABELS[m], color=MODE_COLORS[m], alpha=0.85)
    ax.set_xticks(x + width * (len(modes_ordered) - 1) / 2)
    ax.set_xticklabels([c.replace("_", "\n") for c in combos], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Composite Cost")
    ax.set_title("Cost by Scenario × Fault × Mode")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    fig.savefig(plots_dir / "cost_per_combo.png")
    plt.close(fig)

    # ---- 7. Heatmap: SLO breach % per scenario×fault (RL only vs best baseline) ----
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    scenarios = sorted(df["scenario"].unique())
    faults = sorted(df["fault"].unique())
    for ax_idx, m in enumerate(["rl", "baseline-rolling"]):
        ax = axes[ax_idx]
        matrix = np.zeros((len(scenarios), len(faults)))
        for si, s in enumerate(scenarios):
            for fi, f in enumerate(faults):
                subset = df[(df["mode"] == m) & (df["scenario"] == s) & (df["fault"] == f)]
                matrix[si, fi] = subset["slo_breach"].mean() * 100 if len(subset) > 0 else np.nan
        im = ax.imshow(matrix, cmap="RdYlGn_r", vmin=0, vmax=100, aspect="auto")
        ax.set_xticks(range(len(faults)))
        ax.set_xticklabels(faults, rotation=30, ha="right")
        ax.set_yticks(range(len(scenarios)))
        ax.set_yticklabels(scenarios)
        ax.set_title(f"{MODE_LABELS[m]} — SLO Breach %")
        for si in range(len(scenarios)):
            for fi in range(len(faults)):
                val = matrix[si, fi]
                if not np.isnan(val):
                    ax.text(fi, si, f"{val:.0f}%", ha="center", va="center", fontsize=9,
                            color="white" if val > 50 else "black")
    fig.colorbar(im, ax=axes, shrink=0.6, label="Breach %")
    plt.suptitle("SLO Breach Heatmap: RL vs Rolling Baseline")
    plt.tight_layout()
    fig.savefig(plots_dir / "slo_breach_heatmap.png")
    plt.close(fig)

    # ---- 8. Radar / summary table image ----
    _generate_summary_table(summary_df, plots_dir / "summary_table.png")

    logger.info("Plots saved to %s/", plots_dir)


def _generate_summary_table(summary_df: pd.DataFrame, output_path: Path):
    """Generate a formatted summary table as an image."""
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.axis("off")

    headers = ["Mode", "N", "P95 (ms)", "Error Rate", "Cost", "SLO Breach %"]
    rows = []
    for _, r in summary_df.iterrows():
        rows.append([
            r["label"],
            int(r["n_trials"]),
            f"{r['p95_ms_mean']:.1f} [{r['p95_ms_ci_lo']:.1f}, {r['p95_ms_ci_hi']:.1f}]",
            f"{r['error_rate_mean']:.4f} [{r['error_rate_ci_lo']:.4f}, {r['error_rate_ci_hi']:.4f}]",
            f"{r['cost_mean']:.3f} [{r['cost_ci_lo']:.3f}, {r['cost_ci_hi']:.3f}]",
            f"{r['slo_breach_pct']:.0f}%",
        ])

    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.5)

    # Color header
    for j in range(len(headers)):
        table[0, j].set_facecolor("#4472C4")
        table[0, j].set_text_props(color="white", fontweight="bold")

    # Alternate row colours
    for i in range(1, len(rows) + 1):
        color = "#D9E2F3" if i % 2 == 0 else "white"
        for j in range(len(headers)):
            table[i, j].set_facecolor(color)

    plt.title("Mode Comparison Summary", fontsize=13, fontweight="bold", pad=20)
    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def generate_markdown_report(df: pd.DataFrame, summary_df: pd.DataFrame, pairwise_path: Path, output_dir: Path):
    """Generate a Markdown report with key findings."""
    with open(pairwise_path) as f:
        pairwise = json.load(f)

    lines = [
        "# Orchestrated Rollout — Five-Mode Comparison Results",
        "",
        "## Experimental Setup",
        f"- **Modes**: {', '.join(MODE_LABELS.values())}",
        f"- **Scenarios**: {', '.join(sorted(df['scenario'].unique()))}",
        f"- **Faults**: {', '.join(sorted(df['fault'].unique()))}",
        "- **Trials per combo**: 5",
        f"- **Total trials**: {len(df)}",
        f"- **SLO**: P95 latency < {SLO_P95_LATENCY_MS} ms, error rate < {SLO_ERROR_RATE}",
        "",
        "## Summary Table",
        "",
        "| Mode | N | P95 (ms) | Error Rate | Cost | SLO Breach % |",
        "|------|---|----------|------------|------|-------------|",
    ]

    # Sort by cost ascending to highlight best performer
    for _, r in summary_df.sort_values("cost_mean").iterrows():
        lines.append(
            f"| {r['label']} | {int(r['n_trials'])} | "
            f"{r['p95_ms_mean']:.1f} [{r['p95_ms_ci_lo']:.1f}, {r['p95_ms_ci_hi']:.1f}] | "
            f"{r['error_rate_mean']:.4f} [{r['error_rate_ci_lo']:.4f}, {r['error_rate_ci_hi']:.4f}] | "
            f"{r['cost_mean']:.3f} [{r['cost_ci_lo']:.3f}, {r['cost_ci_hi']:.3f}] | "
            f"{r['slo_breach_pct']:.0f}% |"
        )

    lines += [
        "",
        "## Key Findings",
        "",
    ]

    # Find best / worst
    best = summary_df.loc[summary_df["cost_mean"].idxmin()]
    worst = summary_df.loc[summary_df["cost_mean"].idxmax()]
    rl_row = summary_df[summary_df["mode"] == "rl"]

    lines.append(f"1. **Best performer**: {best['label']} (cost = {best['cost_mean']:.3f})")
    lines.append(f"2. **Worst performer**: {worst['label']} (cost = {worst['cost_mean']:.3f})")

    if not rl_row.empty:
        rl = rl_row.iloc[0]
        rank = list(summary_df.sort_values("cost_mean")["mode"]).index("rl") + 1
        lines.append(f"3. **RL rank**: #{rank} of {len(summary_df)} modes (cost = {rl['cost_mean']:.3f}, SLO breach = {rl['slo_breach_pct']:.0f}%)")

    lines += ["", "## Pairwise Statistical Tests (Mann-Whitney U)", ""]
    lines.append("| Comparison | Metric | Mean A | Mean B | p-value | Sig | Effect (r) |")
    lines.append("|-----------|--------|--------|--------|---------|-----|-----------|")
    for pair_name, metrics_dict in pairwise.items():
        for metric, result in metrics_dict.items():
            lines.append(
                f"| {pair_name} | {metric} | {result['mean_a']:.4f} | {result['mean_b']:.4f} | "
                f"{result['p']:.4f} | {result['sig']} | {result['r']:.3f} |"
            )

    lines += ["", "## Plots", ""]
    for plot_name in ["cost_comparison", "p95_latency_comparison", "error_rate_comparison",
                      "slo_breach_rate", "p95_boxplot", "cost_per_combo", "slo_breach_heatmap",
                      "summary_table"]:
        lines.append(f"![{plot_name}](plots/{plot_name}.png)")
        lines.append("")

    report_path = output_dir / "COMPARISON_REPORT.md"
    report_path.write_text("\n".join(lines))
    logger.info("Markdown report: %s", report_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python -m evaluation.compare_modes <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    experiments_dir = results_dir / "experiments"
    output_dir = results_dir / "reports"

    if not experiments_dir.exists():
        logger.error("Experiments directory not found: %s", experiments_dir)
        sys.exit(1)

    # Load
    df = load_all_trials(experiments_dir)
    if df.empty:
        logger.error("No trial data found!")
        sys.exit(1)

    # Report
    summary_df, _combo_df, scenario_df = generate_report(df, output_dir)

    # Plots
    generate_plots(df, summary_df, scenario_df, output_dir)

    # Markdown
    generate_markdown_report(df, summary_df, output_dir / "pairwise_tests.json", output_dir)

    logger.info("Done! Reports in %s/", output_dir)


if __name__ == "__main__":
    main()

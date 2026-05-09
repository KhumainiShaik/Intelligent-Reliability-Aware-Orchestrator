#!/usr/bin/env python3
"""Generate final dissertation statistics from rich trial metrics.

Outputs:
  - bootstrap_ci_summary.csv
  - per_fault_breakdown.csv
  - per_traffic_breakdown.csv
  - statistical_tests.md

The cost function intentionally matches evaluation.compare_modes:
  SLO breach + latency impact + failed-request impact
with frozen weights 1.0, 0.5, and 5.0.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import stats

SLO_P95_LATENCY_MS = 100.0
SLO_ERROR_RATE = 0.01
W_SLO_VIOLATION = 1.0
W_LATENCY_IMPACT = 0.5
W_ERROR_IMPACT = 5.0

MODE_LABELS = {
    "rl": "RL/adaptive headroom",
    "rl-v11": "RL v11",
    "rl-v12": "RL v12 Contextual",
    "baseline-rolling": "Rolling",
    "baseline-canary": "Canary",
    "baseline-delay": "Delay",
    "baseline-pre-scale": "Pre-Scale",
    "baseline-rule-based": "Rule-based",
}


def _safe_float(value: Any, default: float = math.nan) -> float:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _mode_from_shard(name: str) -> str:
    if "_rl-v11_shard" in name:
        return "rl-v11"
    if "_rl-v12_shard" in name:
        return "rl-v12"
    if "_rl_shard" in name:
        return "rl"
    for mode in ("rolling", "canary", "delay", "pre-scale", "rule-based"):
        if f"_baseline-{mode}_shard" in name:
            return f"baseline-{mode}"
    return "unknown"


def _load_trials(results_dir: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    experiments_dir = results_dir / "experiments"
    for path in sorted(experiments_dir.glob("grid_*_shard*-of-*/*/reports/trial_*_metrics.json")):
        shard = path.parents[2]
        mode = _mode_from_shard(shard.name)
        data = json.loads(path.read_text(encoding="utf-8"))
        http_latency = data.get("http_latency") or {}
        inference_latency = data.get("inference_latency") or {}
        request_count = _safe_float(data.get("request_count"), 0.0)
        failed_requests = _safe_float(data.get("failed_requests"), 0.0)
        http_error_rate = _safe_float(data.get("http_error_rate"), 0.0)
        inference_p95 = _safe_float(inference_latency.get("p95_ms"))
        http_p95 = _safe_float(http_latency.get("p95_ms"))
        p95_for_cost = inference_p95 if not math.isnan(inference_p95) else http_p95
        slo_breach = bool(
            (not math.isnan(p95_for_cost) and p95_for_cost > SLO_P95_LATENCY_MS)
            or http_error_rate > SLO_ERROR_RATE
        )
        latency_norm = (
            float(np.clip((p95_for_cost / SLO_P95_LATENCY_MS - 1.0), 0, 10) / 10.0)
            if not math.isnan(p95_for_cost)
            else 0.0
        )
        error_norm = float(np.clip((http_error_rate / SLO_ERROR_RATE - 1.0), 0, 10) / 10.0)
        corrected_cost = (
            W_SLO_VIOLATION * (1.0 if slo_breach else 0.0)
            + W_LATENCY_IMPACT * latency_norm
            + W_ERROR_IMPACT * error_norm
        )
        rows.append(
            {
                "mode": mode,
                "policy": MODE_LABELS.get(mode, mode),
                "scenario": data.get("scenario"),
                "fault": data.get("fault"),
                "trial": data.get("trial"),
                "phase": data.get("phase"),
                "corrected_cost": corrected_cost,
                "http_p95_ms": http_p95,
                "inference_p95_ms": inference_p95,
                "slo_breach": 1.0 if slo_breach else 0.0,
                "throughput_rps": _safe_float(data.get("throughput_rps")),
                "failed_request_rate": (failed_requests / request_count) if request_count > 0 else 0.0,
                "failed_requests": failed_requests,
                "request_count": request_count,
                "error_rate": http_error_rate,
                "rollout_seconds": _safe_float(data.get("rollout_seconds")),
                "headroom_extra_replicas": _safe_float(data.get("pre_scale_extra_replicas")),
                "chaos_verdict": data.get("chaos_verdict") or "",
            }
        )

    if not rows:
        raise SystemExit(f"No trial metrics found under {experiments_dir}")
    return pd.DataFrame(rows)


def _bootstrap_ci(
    values: pd.Series,
    stat_fn: Callable[[np.ndarray], float] = np.mean,
    n_boot: int = 10_000,
    seed: int = 42,
) -> tuple[float, float, float]:
    arr = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if arr.size == 0:
        return math.nan, math.nan, math.nan
    rng = np.random.default_rng(seed)
    boot = np.array([stat_fn(rng.choice(arr, size=arr.size, replace=True)) for _ in range(n_boot)])
    return float(stat_fn(arr)), float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def _ci_rows(df: pd.DataFrame, grouping: list[str]) -> list[dict[str, Any]]:
    metrics = [
        ("corrected_cost", "mean"),
        ("http_p95_ms", "mean"),
        ("inference_p95_ms", "mean"),
        ("slo_breach", "mean"),
        ("throughput_rps", "mean"),
        ("failed_request_rate", "mean"),
        ("rollout_seconds", "mean"),
        ("headroom_extra_replicas", "mean"),
    ]
    rows: list[dict[str, Any]] = []
    for group_values, group in df.groupby(grouping, dropna=False):
        if not isinstance(group_values, tuple):
            group_values = (group_values,)
        group_fields = dict(zip(grouping, group_values, strict=False))
        for metric, statistic in metrics:
            mean, lo, hi = _bootstrap_ci(group[metric])
            rows.append(
                {
                    **group_fields,
                    "metric": metric,
                    "statistic": statistic,
                    "n": int(group[metric].dropna().shape[0]),
                    "mean": mean,
                    "ci95_low": lo,
                    "ci95_high": hi,
                }
            )
    return rows


def _breakdown(df: pd.DataFrame, key: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    policies = sorted(df["policy"].dropna().unique())
    for value, group in df.groupby(key, dropna=False):
        row: dict[str, Any] = {key: value}
        for policy in policies:
            subset = group[group["policy"] == policy]
            prefix = policy.lower().replace("/", "_").replace(" ", "_").replace("-", "_")
            row[f"{prefix}_trials"] = int(len(subset))
            row[f"{prefix}_cost_mean"] = subset["corrected_cost"].mean()
            row[f"{prefix}_inference_p95_mean"] = subset["inference_p95_ms"].mean()
            row[f"{prefix}_http_p95_mean"] = subset["http_p95_ms"].mean()
            row[f"{prefix}_slo_breach_rate"] = subset["slo_breach"].mean()
            row[f"{prefix}_failed_requests"] = subset["failed_requests"].sum()
            row[f"{prefix}_failed_request_rate"] = (
                subset["failed_requests"].sum() / subset["request_count"].sum()
                if subset["request_count"].sum() > 0
                else 0.0
            )
            row[f"{prefix}_throughput_mean"] = subset["throughput_rps"].mean()
            row[f"{prefix}_rollout_seconds_mean"] = subset["rollout_seconds"].mean()
            row[f"{prefix}_headroom_mean"] = subset["headroom_extra_replicas"].mean()

        if {"RL/adaptive headroom", "Pre-Scale"}.issubset(set(policies)):
            rl = group[group["policy"] == "RL/adaptive headroom"]
            ps = group[group["policy"] == "Pre-Scale"]
            row["p95_difference_ms_rl_minus_prescale"] = (
                rl["inference_p95_ms"].mean() - ps["inference_p95_ms"].mean()
            )
            row["cost_difference_rl_minus_prescale"] = (
                rl["corrected_cost"].mean() - ps["corrected_cost"].mean()
            )
            row["interpretation"] = _interpret_breakdown(str(value), key)
        rows.append(row)
    return pd.DataFrame(rows).sort_values(key).reset_index(drop=True)


def _interpret_breakdown(value: str, key: str) -> str:
    if key == "fault":
        return {
            "none": "autoscaling-only behaviour",
            "pod-kill": "resilience under replica loss",
            "network-latency": "degradation under network stress",
        }.get(value, "")
    return {
        "ramp": "HPA catch-up under gradually rising load",
        "spike": "fast autoscaling stress under sudden load",
    }.get(value, "")


def _mann_whitney(a: pd.Series, b: pd.Series) -> tuple[float, float, float, str]:
    aa = pd.to_numeric(a, errors="coerce").dropna().to_numpy(dtype=float)
    bb = pd.to_numeric(b, errors="coerce").dropna().to_numpy(dtype=float)
    if aa.size < 2 or bb.size < 2:
        return math.nan, 1.0, 0.0, "n/a"
    u, p = stats.mannwhitneyu(aa, bb, alternative="two-sided")
    r = 1 - 2 * float(u) / (aa.size * bb.size)
    sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else "ns"))
    return float(u), float(p), float(r), sig


def _statistical_tests_markdown(df: pd.DataFrame) -> str:
    lines = [
        "# Final Statistical Tests",
        "",
        "Frozen cost function: SLO breach + latency impact + failed-request impact "
        "(weights 1.0, 0.5, 5.0). Significance level: alpha = 0.05.",
        "",
    ]
    policies = sorted(df["policy"].dropna().unique())
    if len(policies) < 2:
        lines.append("Only one policy found; pairwise tests were skipped.")
        return "\n".join(lines) + "\n"

    metrics = [
        "corrected_cost",
        "http_p95_ms",
        "inference_p95_ms",
        "slo_breach",
        "throughput_rps",
        "failed_request_rate",
        "rollout_seconds",
        "headroom_extra_replicas",
    ]
    lines += [
        "## Pairwise Mann-Whitney U Tests",
        "",
        "| Comparison | Metric | Mean A | Mean B | U | p-value | Sig | Effect r |",
        "|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for i, policy_a in enumerate(policies):
        for policy_b in policies[i + 1 :]:
            a = df[df["policy"] == policy_a]
            b = df[df["policy"] == policy_b]
            for metric in metrics:
                u, p, r, sig = _mann_whitney(a[metric], b[metric])
                lines.append(
                    f"| {policy_a} vs {policy_b} | {metric} | "
                    f"{a[metric].mean():.6f} | {b[metric].mean():.6f} | "
                    f"{u:.3f} | {p:.6g} | {sig} | {r:.3f} |"
                )

    lines += [
        "",
        "## Interpretation",
        "",
        "- A significant corrected-cost or P95 result supports SLO-impact improvement.",
        "- A non-significant failed-request-rate result means both policies should be described as near-zero-error, not as a proven error-rate difference.",
        "- Headroom differences are expected when adaptive headroom uses more pre-scale capacity than fixed baselines.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", type=Path)
    args = parser.parse_args()

    reports_dir = args.results_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    df = _load_trials(args.results_dir)

    ci_rows = []
    ci_rows.extend(_ci_rows(df, ["policy"]))
    ci_rows.extend(_ci_rows(df, ["policy", "scenario"]))
    ci_rows.extend(_ci_rows(df, ["policy", "fault"]))
    pd.DataFrame(ci_rows).to_csv(reports_dir / "bootstrap_ci_summary.csv", index=False)

    _breakdown(df, "fault").to_csv(reports_dir / "per_fault_breakdown.csv", index=False)
    _breakdown(df, "scenario").to_csv(reports_dir / "per_traffic_breakdown.csv", index=False)
    (reports_dir / "statistical_tests.md").write_text(
        _statistical_tests_markdown(df), encoding="utf-8"
    )

    print(f"wrote {reports_dir / 'bootstrap_ci_summary.csv'}")
    print(f"wrote {reports_dir / 'per_fault_breakdown.csv'}")
    print(f"wrote {reports_dir / 'per_traffic_breakdown.csv'}")
    print(f"wrote {reports_dir / 'statistical_tests.md'}")


if __name__ == "__main__":
    main()

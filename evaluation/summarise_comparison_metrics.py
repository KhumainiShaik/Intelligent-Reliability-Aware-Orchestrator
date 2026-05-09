#!/usr/bin/env python3
"""Aggregate rich per-trial metrics for a comparison results directory.

The main ``compare_modes`` report reads k6 summary exports so it can work with
older runs. Newer runs also write ``reports/trial_*_metrics.json`` files with
deployment timing, throughput, request counts, and chaos verdicts. This script
turns those richer files into CSV/JSON tables suitable for final reporting.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

MODE_LABELS = {
    "rl": os.environ.get("RL_MODE_LABEL", "RL / Adaptive"),
    "rl-v11": "RL v11",
    "rl-v12": "RL v12 Contextual",
    "baseline-rolling": "Rolling",
    "baseline-canary": "Canary",
    "baseline-delay": "Delay",
    "baseline-pre-scale": "Pre-Scale",
    "baseline-rule-based": "Rule-based",
}


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mode_from_shard(shard_name: str) -> str:
    if "_rl-v11_shard" in shard_name:
        return "rl-v11"
    if "_rl-v12_shard" in shard_name:
        return "rl-v12"
    if "_rl_shard" in shard_name:
        return "rl"
    for mode in ("rolling", "canary", "delay", "pre-scale", "rule-based"):
        marker = f"_baseline-{mode}_shard"
        if marker in shard_name:
            return f"baseline-{mode}"
    return "unknown"


def _mix(values: pd.Series) -> str:
    counts = values.dropna()
    counts = counts[counts.astype(str) != ""].astype(str).value_counts().sort_index()
    return ";".join(f"{name}={count}" for name, count in counts.items())


def _load_trial_rows(experiments_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metrics_path in sorted(experiments_dir.glob("grid_*_shard*-of-*/*/reports/trial_*_metrics.json")):
        shard_dir = metrics_path.parents[2]
        mode = _mode_from_shard(shard_dir.name)
        try:
            data = json.loads(metrics_path.read_text())
        except json.JSONDecodeError:
            continue

        http_latency = data.get("http_latency") or {}
        inference_latency = data.get("inference_latency") or {}
        request_count = _safe_float(data.get("request_count"))
        failed_requests = _safe_float(data.get("failed_requests")) or 0.0

        rows.append({
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "shard": shard_dir.name,
            "scenario": data.get("scenario"),
            "fault": data.get("fault"),
            "trial": data.get("trial"),
            "phase": data.get("phase"),
            "strategy": data.get("strategy"),
            "policy_version": data.get("policy_version"),
            "pre_scale_extra_replicas": _safe_float(data.get("pre_scale_extra_replicas")),
            "stress_score": _safe_float(data.get("stress_score")),
            "rollout_seconds": _safe_float(data.get("rollout_seconds")),
            "k6_wall_seconds": _safe_float(data.get("k6_wall_seconds")),
            "trial_seconds": _safe_float(data.get("trial_seconds")),
            "reset_seconds": _safe_float(data.get("reset_seconds")),
            "model_load_seconds": _safe_float(data.get("model_load_seconds")),
            "request_count": request_count,
            "failed_requests": failed_requests,
            "failed_request_pct": (failed_requests / request_count * 100.0)
            if request_count and request_count > 0
            else None,
            "http_error_rate_pct": (_safe_float(data.get("http_error_rate")) or 0.0) * 100.0,
            "custom_error_rate_pct": (_safe_float(data.get("custom_error_rate")) or 0.0) * 100.0,
            "throughput_rps": _safe_float(data.get("throughput_rps")),
            "http_p95_ms": _safe_float(http_latency.get("p95_ms")),
            "http_p99_ms": _safe_float(http_latency.get("p99_ms")),
            "inference_p95_ms": _safe_float(inference_latency.get("p95_ms")),
            "inference_p99_ms": _safe_float(inference_latency.get("p99_ms")),
            "chaos_verdict": data.get("chaos_verdict"),
        })
    return rows


def _summarise(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (mode, label), group in df.groupby(["mode", "label"], dropna=False):
        request_total = float(group["request_count"].fillna(0).sum())
        failed_total = float(group["failed_requests"].fillna(0).sum())
        rows.append({
            "mode": mode,
            "label": label,
            "trials": int(len(group)),
            "completed_trials": int((group["phase"] == "Completed").sum()),
            "strategy_mix": _mix(group["strategy"]),
            "policy_versions": _mix(group["policy_version"]),
            "pre_scale_extra_mix": _mix(group["pre_scale_extra_replicas"]),
            "chaos_verdicts": _mix(group["chaos_verdict"]),
            "total_requests": request_total,
            "failed_requests": failed_total,
            "failed_request_pct": (failed_total / request_total * 100.0)
            if request_total > 0
            else None,
            "mean_http_error_rate_pct": group["http_error_rate_pct"].mean(),
            "mean_throughput_rps": group["throughput_rps"].mean(),
            "mean_rollout_seconds": group["rollout_seconds"].mean(),
            "mean_k6_wall_seconds": group["k6_wall_seconds"].mean(),
            "mean_trial_seconds": group["trial_seconds"].mean(),
            "mean_reset_seconds": group["reset_seconds"].mean(),
            "mean_model_load_seconds": group["model_load_seconds"].dropna().mean(),
            "mean_http_p95_ms": group["http_p95_ms"].mean(),
            "mean_http_p99_ms": group["http_p99_ms"].mean(),
            "mean_inference_p95_ms": group["inference_p95_ms"].mean(),
            "mean_inference_p99_ms": group["inference_p99_ms"].mean(),
        })
    return pd.DataFrame(rows).sort_values(["mode"]).reset_index(drop=True)


def _decision_distribution(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "mode",
        "label",
        "strategy",
        "policy_version",
        "pre_scale_extra_replicas",
        "trials",
    ]
    if df.empty:
        return pd.DataFrame(columns=columns)
    return (
        df.groupby(
            [
                "mode",
                "label",
                "strategy",
                "policy_version",
                "pre_scale_extra_replicas",
            ],
            dropna=False,
        )
        .size()
        .reset_index(name="trials")
        .sort_values(["mode", "strategy", "policy_version", "pre_scale_extra_replicas"])
        .reset_index(drop=True)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", type=Path)
    args = parser.parse_args()

    experiments_dir = args.results_dir / "experiments"
    reports_dir = args.results_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_trial_rows(experiments_dir)
    if not rows:
        raise SystemExit(f"No trial metrics found under {experiments_dir}")

    trial_df = pd.DataFrame(rows)
    summary_df = _summarise(trial_df)
    decision_df = _decision_distribution(trial_df)

    trial_df.to_csv(reports_dir / "targeted_trial_metrics.csv", index=False)
    summary_df.to_csv(reports_dir / "targeted_metrics_summary.csv", index=False)
    decision_df.to_csv(reports_dir / "decision_distribution.csv", index=False)
    (reports_dir / "targeted_metrics_summary.json").write_text(
        json.dumps(summary_df.to_dict(orient="records"), indent=2)
    )

    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()

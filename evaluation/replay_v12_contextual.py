#!/usr/bin/env python3
"""Replay v12 contextual decisions against completed trial contexts."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from controller.reconciler import _v12_contextual_decision
from controller.snapshot import DecisionSnapshot


def _float(value: str | None, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _same_headroom(left: int | None, right: str) -> bool:
    if left is None:
        return right in ("", None)
    return _float(right, -999.0) == float(left)


def _snapshot(row: dict[str, str]) -> DecisionSnapshot:
    return DecisionSnapshot(
        rps=_float(row.get("throughput_rps")),
        p95_latency_ms=_float(row.get("http_p95_ms")),
        p99_latency_ms=_float(row.get("http_p99_ms")),
        error_rate=_float(row.get("http_error_rate_pct")) / 100.0,
        pending_pods=0,
        node_cpu_util=0.0,
        node_mem_util=0.0,
        hpa_desired_replicas=0,
        hpa_current_replicas=0,
    )


def replay(reports_dir: Path) -> None:
    source = reports_dir / "targeted_trial_metrics.csv"
    rows: list[dict[str, str]] = []
    with source.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            # Use the validated RL rows as the contexts that v12 is intended to improve.
            if row.get("mode") == "rl":
                rows.append(row)

    decisions: list[dict[str, str]] = []
    for row in rows:
        hints = {
            "trafficProfile": row.get("scenario", ""),
            "faultContext": row.get("fault", ""),
            "objective": "reliability",
            "policyVariant": "v12-contextual",
        }
        action, extra, reason = _v12_contextual_decision(
            _snapshot(row),
            _float(row.get("stress_score")),
            hints,
            {"maxExtraReplicas": 7, "maxDelaySeconds": 30},
        )
        decisions.append(
            {
                "scenario": row.get("scenario", ""),
                "fault": row.get("fault", ""),
                "trial": row.get("trial", ""),
                "v11_strategy": row.get("strategy", ""),
                "v11_headroom": row.get("pre_scale_extra_replicas", ""),
                "v12_strategy": action,
                "v12_headroom": "" if extra is None else str(extra),
                "differs_from_v11": str(
                    action != row.get("strategy", "")
                    or not _same_headroom(extra, row.get("pre_scale_extra_replicas", ""))
                ).lower(),
                "v12_reason": reason,
            }
        )

    distribution_rows: list[dict[str, str]] = []
    counts = Counter(
        (row["scenario"], row["fault"], row["v12_strategy"], row["v12_headroom"])
        for row in decisions
    )
    for (scenario, fault, strategy, headroom), count in sorted(counts.items()):
        distribution_rows.append(
            {
                "scenario": scenario,
                "fault": fault,
                "v12_strategy": strategy,
                "v12_headroom": headroom,
                "trials": str(count),
            }
        )

    replay_path = reports_dir / "v12_vs_v11_offline_replay.csv"
    with replay_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(decisions[0].keys()))
        writer.writeheader()
        writer.writerows(decisions)

    distribution_path = reports_dir / "v12_offline_decision_distribution.csv"
    with distribution_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(distribution_rows[0].keys()))
        writer.writeheader()
        writer.writerows(distribution_rows)

    print(f"wrote {replay_path}")
    print(f"wrote {distribution_path}")
    print(f"decisions={len(decisions)}")
    print(f"changed={sum(1 for row in decisions if row['differs_from_v11'] == 'true')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "reports_dir",
        type=Path,
        default=Path("results/comparison_20260508_balanced_actionset_72/reports"),
        nargs="?",
    )
    args = parser.parse_args()
    replay(args.reports_dir)


if __name__ == "__main__":
    main()

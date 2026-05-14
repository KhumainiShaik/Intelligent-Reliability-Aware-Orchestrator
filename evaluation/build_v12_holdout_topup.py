#!/usr/bin/env python3
"""Build a v12 holdout top-up directory from an existing repeat-1 holdout."""

from __future__ import annotations

import argparse
import csv
import shutil
from collections import Counter
from pathlib import Path

MODES = ["rl-v11", "rl-v12", "rule-based", "rolling", "pre-scale"]
SCENARIOS = ["ramp", "spike"]
FAULTS = ["none", "pod-kill", "network-latency"]


def mode_token(mode: str) -> str:
    if mode in {"rl-v11", "rl-v12"}:
        return mode
    return f"baseline-{mode}"


def combo_index(scenario: str, fault: str) -> int:
    return SCENARIOS.index(scenario) * len(FAULTS) + FAULTS.index(fault)


def source_timestamp(source: Path) -> str:
    return source.name.removeprefix("comparison_")


def safe_link(source: Path, dest: Path) -> bool:
    if dest.exists() or dest.is_symlink():
        return True
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source.resolve())
    return True


def link_matching(source_dir: Path, dest_dir: Path, pattern: str) -> int:
    if not source_dir.exists():
        return 0
    count = 0
    for source in sorted(source_dir.glob(pattern)):
        if source.is_file() and safe_link(source, dest_dir / source.name):
            count += 1
    return count


def link_trial(source_combo: Path, dest_combo: Path, repeat: int) -> bool:
    metrics = source_combo / "reports" / f"trial_{repeat}_metrics.json"
    if not safe_link(metrics, dest_combo / "reports" / metrics.name):
        return False
    link_matching(source_combo / "reports", dest_combo / "reports", f"trial_{repeat}_*")
    link_matching(source_combo / "episodes", dest_combo / "episodes", f"trial_{repeat}_*")
    link_matching(source_combo / "k6_results", dest_combo / "k6_results", f"trial_{repeat}_*")
    return True


def build(source: Path, output: Path, target_repeats: int, shard_count: int, force: bool) -> None:
    if output.exists() and force:
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "experiments").mkdir(exist_ok=True)
    (output / "reports").mkdir(exist_ok=True)

    src_ts = source_timestamp(source)
    dst_ts = source_timestamp(output)

    reused = 0
    missing_rows: list[dict[str, str | int]] = []
    missing_by_mode: Counter[str] = Counter()
    missing_by_scenario: Counter[str] = Counter()
    missing_by_fault: Counter[str] = Counter()

    for mode in MODES:
        token = mode_token(mode)
        for scenario in SCENARIOS:
            for fault in FAULTS:
                shard = combo_index(scenario, fault) % shard_count
                src_combo = (
                    Path("experiments")
                    / f"grid_{src_ts}_{token}_shard{shard}-of-{shard_count}"
                    / f"{scenario}_{fault}"
                )
                dst_combo = (
                    output
                    / "experiments"
                    / f"grid_{dst_ts}_{token}_shard{shard}-of-{shard_count}"
                    / f"{scenario}_{fault}"
                )
                for repeat in range(1, target_repeats + 1):
                    if repeat == 1 and link_trial(src_combo, dst_combo, 1):
                        reused += 1
                        continue
                    missing_rows.append(
                        {
                            "mode": mode,
                            "scenario": scenario,
                            "fault": fault,
                            "repeat": repeat,
                            "shard": shard,
                            "status": "missing",
                        }
                    )
                    missing_by_mode[mode] += 1
                    missing_by_scenario[scenario] += 1
                    missing_by_fault[fault] += 1

    with (output / "missing_trials.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["mode", "scenario", "fault", "repeat", "shard", "status"]
        )
        writer.writeheader()
        writer.writerows(missing_rows)

    with (output / "topup_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item", "value"])
        writer.writerow(["source", source])
        writer.writerow(["output", output])
        writer.writerow(["target_repeats", target_repeats])
        writer.writerow(
            ["target_trials", len(MODES) * len(SCENARIOS) * len(FAULTS) * target_repeats]
        )
        writer.writerow(["reused_trials", reused])
        writer.writerow(["missing_trials", len(missing_rows)])

    print(f"output={output}")
    print(f"reused_trials={reused}")
    print(f"missing_trials={len(missing_rows)}")
    print("missing_by_mode")
    for mode in MODES:
        print(f"  {mode}: {missing_by_mode[mode]}")
    print("missing_by_scenario")
    for scenario in SCENARIOS:
        print(f"  {scenario}: {missing_by_scenario[scenario]}")
    print("missing_by_fault")
    for fault in FAULTS:
        print(f"  {fault}: {missing_by_fault[fault]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path("results/comparison_20260508_v12_holdout_30")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/comparison_20260512_v12_holdout_60")
    )
    parser.add_argument("--target-repeats", type=int, default=2)
    parser.add_argument("--shard-count", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.target_repeats != 2:
        raise SystemExit("v12 top-up builder currently expects --target-repeats 2")
    build(args.source, args.output, args.target_repeats, args.shard_count, args.force)


if __name__ == "__main__":
    main()

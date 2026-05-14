#!/usr/bin/env python3
"""Build a balanced action-set result directory from completed trial artifacts."""

from __future__ import annotations

import argparse
import csv
import shutil
from collections import Counter
from pathlib import Path

MODES = ["rl", "rule-based", "rolling", "canary", "pre-scale", "delay"]
SCENARIOS = ["ramp", "spike"]
FAULTS = ["none", "pod-kill", "network-latency"]


def _mode_token(mode: str) -> str:
    return "rl" if mode == "rl" else f"baseline-{mode}"


def _mode_from_shard(shard_name: str) -> str | None:
    if "_rl_shard" in shard_name:
        return "rl"
    for mode in MODES:
        if mode == "rl":
            continue
        if f"_baseline-{mode}_shard" in shard_name:
            return mode
    return None


def _combo_index(scenario: str, fault: str) -> int:
    return SCENARIOS.index(scenario) * len(FAULTS) + FAULTS.index(fault)


def _safe_link_file(source: Path, dest: Path) -> bool:
    if dest.exists() or dest.is_symlink():
        return True
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source.resolve())
    return True


def _safe_link_matching(source_dir: Path, dest_dir: Path, pattern: str) -> int:
    if not source_dir.exists():
        return 0
    linked = 0
    for source in sorted(source_dir.glob(pattern)):
        if source.is_file() and _safe_link_file(source, dest_dir / source.name):
            linked += 1
    return linked


def _safe_link_aux_dir(source: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source.resolve())


def _link_trial_artifacts(source_combo: Path, dest_combo: Path, repeat: int) -> bool:
    """Link only the target repeat's artifacts to keep the balanced matrix exact."""
    metrics = source_combo / "reports" / f"trial_{repeat}_metrics.json"
    if not _safe_link_file(metrics, dest_combo / "reports" / metrics.name):
        return False

    _safe_link_matching(
        source_combo / "reports",
        dest_combo / "reports",
        f"trial_{repeat}_*",
    )
    _safe_link_matching(
        source_combo / "k6_results",
        dest_combo / "k6_results",
        f"trial_{repeat}_*",
    )
    _safe_link_matching(
        source_combo / "episodes",
        dest_combo / "episodes",
        f"trial_{repeat}_*",
    )

    # Optional run-level artifacts are useful for audit but are not trial rows.
    for child_name in ("manifests",):
        src_child = source_combo / child_name
        if src_child.exists():
            _safe_link_aux_dir(src_child, dest_combo / child_name)
    return True


def _discover_completed(source_timestamp: str) -> dict[tuple[str, str, str, int], Path]:
    completed: dict[tuple[str, str, str, int], Path] = {}
    pattern = f"grid_{source_timestamp}_*_shard*-of-*/*/reports/trial_*_metrics.json"
    for metrics in sorted(Path("experiments").glob(pattern)):
        shard_dir = metrics.parents[2]
        combo_dir = metrics.parents[1]
        mode = _mode_from_shard(shard_dir.name)
        if mode is None:
            continue
        try:
            scenario, fault = combo_dir.name.split("_", 1)
            repeat = int(metrics.stem.replace("trial_", "").replace("_metrics", ""))
        except ValueError:
            continue
        if scenario in SCENARIOS and fault in FAULTS:
            completed[(mode, scenario, fault, repeat)] = combo_dir
    return completed


def _source_timestamp(source: Path) -> str:
    name = source.name
    if name.startswith("comparison_"):
        return name.removeprefix("comparison_")
    return name


def build(source: Path, output: Path, target_repeats: int, shard_count: int) -> None:
    timestamp = output.name.removeprefix("comparison_")
    completed = _discover_completed(_source_timestamp(source))
    experiments_out = output / "experiments"
    reports_out = output / "reports"
    experiments_out.mkdir(parents=True, exist_ok=True)
    reports_out.mkdir(parents=True, exist_ok=True)

    selected = 0
    missing_rows: list[dict[str, str | int]] = []
    selected_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()
    missing_scenarios: Counter[str] = Counter()
    missing_faults: Counter[str] = Counter()

    for mode in MODES:
        token = _mode_token(mode)
        for scenario in SCENARIOS:
            for fault in FAULTS:
                shard = _combo_index(scenario, fault) % shard_count
                dest_combo = (
                    experiments_out
                    / f"grid_{timestamp}_{token}_shard{shard}-of-{shard_count}"
                    / f"{scenario}_{fault}"
                )
                for repeat in range(1, target_repeats + 1):
                    source_combo = completed.get((mode, scenario, fault, repeat))
                    if source_combo is None:
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
                        missing_counts[mode] += 1
                        missing_scenarios[scenario] += 1
                        missing_faults[fault] += 1
                        continue

                    dest_combo.mkdir(parents=True, exist_ok=True)
                    if not _link_trial_artifacts(source_combo, dest_combo, repeat):
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
                        missing_counts[mode] += 1
                        missing_scenarios[scenario] += 1
                        missing_faults[fault] += 1
                        continue
                    selected += 1
                    selected_counts[mode] += 1

    missing_csv = output / "missing_trials.csv"
    with missing_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["mode", "scenario", "fault", "repeat", "shard", "status"],
        )
        writer.writeheader()
        writer.writerows(missing_rows)

    summary_csv = output / "balanced_matrix_summary.csv"
    with summary_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item", "value"])
        writer.writerow(["target_repeats", target_repeats])
        writer.writerow(
            ["target_trials", len(MODES) * len(SCENARIOS) * len(FAULTS) * target_repeats]
        )
        writer.writerow(["selected_completed_trials", selected])
        writer.writerow(["missing_trials", len(missing_rows)])
        for mode in MODES:
            writer.writerow([f"selected_{mode}", selected_counts[mode]])
            writer.writerow([f"missing_{mode}", missing_counts[mode]])
        for scenario in SCENARIOS:
            writer.writerow([f"missing_scenario_{scenario}", missing_scenarios[scenario]])
        for fault in FAULTS:
            writer.writerow([f"missing_fault_{fault}", missing_faults[fault]])

    print(f"output={output}")
    print(f"target_repeats={target_repeats}")
    print(f"selected_completed_trials={selected}")
    print(f"missing_trials={len(missing_rows)}")
    print("missing_by_mode")
    for mode in MODES:
        print(f"  {mode}: {missing_counts[mode]}")
    print("missing_by_scenario")
    for scenario in SCENARIOS:
        print(f"  {scenario}: {missing_scenarios[scenario]}")
    print("missing_by_fault")
    for fault in FAULTS:
        print(f"  {fault}: {missing_faults[fault]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path("results/comparison_20260507_full_actionset_108")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/comparison_20260508_balanced_actionset_72")
    )
    parser.add_argument("--target-repeats", type=int, default=2)
    parser.add_argument("--shard-count", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.output.exists() and args.force:
        shutil.rmtree(args.output)
    if args.target_repeats < 1:
        raise SystemExit("--target-repeats must be >= 1")
    build(args.source, args.output, args.target_repeats, args.shard_count)


if __name__ == "__main__":
    main()

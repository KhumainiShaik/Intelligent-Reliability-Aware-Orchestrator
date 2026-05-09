#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_artifact = repo_root / "artifacts" / "v11_no_forecast" / "policy_artifact.json"

    ap = argparse.ArgumentParser(
        description="Summarise a policy_artifact.json (version, features, actions, training summary)."
    )
    ap.add_argument(
        "--artifact",
        type=Path,
        default=default_artifact,
        help=f"Path to policy_artifact.json (default: {default_artifact})",
    )
    ap.add_argument(
        "--show-sample-states",
        type=int,
        default=0,
        help="Print the first N Q-table state keys (default: 0).",
    )
    args = ap.parse_args()

    p: Path = args.artifact
    if not p.exists():
        print(f"ERROR: file not found: {p}", file=sys.stderr)
        return 2

    try:
        obj = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON: {p}: {exc}", file=sys.stderr)
        return 2

    version = obj.get("version")
    features = obj.get("features", []) or []
    bins = obj.get("bins", {}) or {}
    actions = obj.get("actions", []) or []
    q_table = obj.get("q_table", {}) or {}

    print(f"artifact: {p}")
    print(f"version: {version}")
    print(f"actions: {actions}")
    print(f"states_in_q_table: {len(q_table)}")

    print("\nfeatures:")
    if not features:
        print("  (none)")
    else:
        for f in features:
            name = f.get("name")
            num_bins = f.get("num_bins")
            edges = bins.get(name, [])
            print(f"  - {name}: num_bins={num_bins}  bin_edges={edges}")

    ts = obj.get("training_summary", {}) or {}
    if ts:
        print("\ntraining_summary:")
        for k in [
            "total_episodes",
            "unique_states",
            "mean_cost",
            "final_mean_cost",
            "std_cost",
            "final_epsilon",
        ]:
            if k in ts:
                print(f"  - {k}: {_fmt(ts[k])}")

        ad = ts.get("action_distribution", {}) or {}
        if ad:
            print("\naction_distribution:")
            for k, v in sorted(ad.items(), key=lambda kv: (-float(kv[1]), str(kv[0]))):
                try:
                    print(f"  - {k}: {float(v):.3f}")
                except (TypeError, ValueError):
                    print(f"  - {k}: {v}")

    n = int(args.show_sample_states)
    if n > 0 and q_table:
        print("\nsample_q_table_states:")
        for i, key in enumerate(q_table.keys()):
            if i >= n:
                break
            print(f"  - {key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

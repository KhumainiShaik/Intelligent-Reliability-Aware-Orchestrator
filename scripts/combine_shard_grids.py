#!/usr/bin/env python3
"""Combine per-shard grid outputs into a single analysis-ready directory.

This is intended for runs launched via scripts/gke/10_run_grid_shards.sh, where each
shard produces a directory like:
  experiments/grid_<TIMESTAMP>_shard<idx>-of-<N>/<scenario>_<fault>/...

To avoid duplicating large artifacts (k6 logs, port-forward logs, etc.), this script
creates a *minimal* combined directory containing only symlinks to episode JSON files:
  <combined>/<scenario>_<fault>/episodes/episode_*.json -> <original>

Usage:
  python3 scripts/combine_shard_grids.py 20260403_112530_rl
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path


def _pick_output_dir(experiments_dir: Path, timestamp: str) -> Path:
    base = experiments_dir / f"grid_{timestamp}_all_min"
    if not base.exists():
        return base
    suffix = dt.datetime.now().strftime("%H%M%S")
    return experiments_dir / f"grid_{timestamp}_all_min_{suffix}"


def combine(timestamp: str, experiments_dir: Path, output_dir: Path | None) -> Path:
    shard_glob = f"grid_{timestamp}_shard*-of-*"
    shard_dirs = sorted(experiments_dir.glob(shard_glob))
    if not shard_dirs:
        raise SystemExit(f"No shard grid dirs found under {experiments_dir}/{shard_glob}")

    combined_dir = output_dir or _pick_output_dir(experiments_dir, timestamp)
    combined_dir.mkdir(parents=True, exist_ok=False)

    combos_seen: set[str] = set()
    episode_links = 0

    for shard_dir in shard_dirs:
        for combo_dir in shard_dir.iterdir():
            if not combo_dir.is_dir():
                continue

            combo_name = combo_dir.name
            if combo_name in combos_seen:
                raise SystemExit(f"Duplicate combo {combo_name!r} found across shards")
            combos_seen.add(combo_name)

            src_eps_dir = combo_dir / "episodes"
            if not src_eps_dir.exists():
                raise SystemExit(f"Missing episodes dir: {src_eps_dir}")

            dest_eps_dir = combined_dir / combo_name / "episodes"
            dest_eps_dir.mkdir(parents=True)

            for ep in sorted(src_eps_dir.glob("episode_*.json")):
                dest = dest_eps_dir / ep.name
                os.symlink(ep.resolve(), dest)
                episode_links += 1

    print(f"combined_dir: {combined_dir}")
    print(f"combos:        {len(combos_seen)}")
    print(f"episode_links: {episode_links}")
    return combined_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("timestamp", help="The GRID_TIMESTAMP used for the shard run")
    parser.add_argument("--experiments-dir", default="experiments", help="Base experiments directory")
    parser.add_argument("--output-dir", default=None, help="Optional output directory")
    args = parser.parse_args()

    experiments_dir = Path(args.experiments_dir)
    output_dir = Path(args.output_dir) if args.output_dir else None

    combine(args.timestamp, experiments_dir=experiments_dir, output_dir=output_dir)


if __name__ == "__main__":
    main()

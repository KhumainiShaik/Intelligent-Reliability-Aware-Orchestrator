"""Final action-set validation tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from evaluation.final_statistics import MODE_LABELS as FINAL_STAT_MODE_LABELS
from evaluation.final_statistics import _mode_from_shard as final_stats_mode_from_shard
from evaluation.summarise_comparison_metrics import MODE_LABELS as SUMMARY_MODE_LABELS
from evaluation.summarise_comparison_metrics import _decision_distribution
from evaluation.summarise_comparison_metrics import _mode_from_shard as summary_mode_from_shard
import pandas as pd


FINAL_MODES = ["rl", "rule-based", "rolling", "canary", "pre-scale", "delay"]
BASELINE_MODES = ["rolling", "canary", "pre-scale", "delay", "rule-based"]


def _action_set_enum(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec_props = data["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"][
        "spec"
    ]["properties"]
    return spec_props["actionSet"]["items"]["enum"]


def test_crd_action_set_accepts_all_final_modes() -> None:
    for crd_path in (
        Path("k8s/controller/crd/orchestratedrollout-crd.yaml"),
        Path("charts/controller/crds/orchestratedrollout-crd.yaml"),
    ):
        enum_values = _action_set_enum(crd_path)
        for mode in FINAL_MODES:
            assert mode in enum_values


def test_statistics_generators_include_all_six_modes() -> None:
    expected_keys = {"rl"} | {f"baseline-{mode}" for mode in BASELINE_MODES}

    assert expected_keys.issubset(FINAL_STAT_MODE_LABELS)
    assert expected_keys.issubset(SUMMARY_MODE_LABELS)

    assert final_stats_mode_from_shard("grid_ts_rl_shard0-of-3") == "rl"
    assert summary_mode_from_shard("grid_ts_rl_shard0-of-3") == "rl"
    for mode in BASELINE_MODES:
        shard = f"grid_ts_baseline-{mode}_shard0-of-3"
        assert final_stats_mode_from_shard(shard) == f"baseline-{mode}"
        assert summary_mode_from_shard(shard) == f"baseline-{mode}"


def test_decision_distribution_includes_strategy_and_headroom() -> None:
    distribution = _decision_distribution(
        pd.DataFrame(
            [
                {
                    "mode": "rl",
                    "label": "RL / Adaptive",
                    "strategy": "pre-scale",
                    "policy_version": "v11+adaptive-capacity",
                    "pre_scale_extra_replicas": 5,
                },
                {
                    "mode": "baseline-delay",
                    "label": "Delay",
                    "strategy": "delay",
                    "policy_version": "baseline-delay",
                    "pre_scale_extra_replicas": None,
                },
            ]
        )
    )

    assert {
        "mode",
        "strategy",
        "policy_version",
        "pre_scale_extra_replicas",
        "trials",
    }.issubset(distribution.columns)
    assert set(distribution["strategy"]) == {"pre-scale", "delay"}

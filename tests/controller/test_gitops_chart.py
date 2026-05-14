"""GitOps Helm rendering tests for the portable workload chart."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_cpu_gitops_values_render_policy_selected_orchestrated_rollout() -> None:
    """The GitOps values render rollout context, not a fixed strategy decision."""
    repo_root = Path(__file__).resolve().parents[2]
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "cpu-demo",
            str(repo_root / "charts/portable-workload"),
            "-f",
            str(repo_root / "gitops/workloads/cpu-bound-fastapi/values.yaml"),
        ],
        text=True,
    )

    docs = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    oroll = next(doc for doc in docs if doc.get("kind") == "OrchestratedRollout")
    spec = oroll["spec"]

    assert spec["actionSet"] == ["rl"]
    assert spec["rolloutHints"]["trafficProfile"] == "ramp"
    assert spec["rolloutHints"]["objective"] == "reliability"
    assert spec["rolloutHints"]["policyVariant"] == "v12-contextual"
    assert spec["rolloutHints"]["faultContext"] == "none"
    assert "strategy" not in spec
    assert "chosenStrategy" not in spec
    assert "preScaleExtraReplicas" not in spec

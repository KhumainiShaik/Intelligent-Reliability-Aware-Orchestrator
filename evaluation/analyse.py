"""
Evaluation and Statistical Analysis Pipeline.

Computes:
- Bootstrap confidence intervals
- Mann-Whitney U tests for policy comparisons
- Ablation analysis (feature/action removal)
- Result visualisations and summary tables
"""

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


DEFAULT_SLO_P95_LATENCY_MS = float(os.environ.get("SLO_P95_LATENCY_MS", "100"))
DEFAULT_SLO_ERROR_RATE = float(os.environ.get("SLO_ERROR_RATE", "0.01"))

# Cost weights (aligned with docs/codebase/06_EXPERIMENTS_AND_SCRIPTS.md).
W_SLO_VIOLATION = float(os.environ.get("W_SLO_VIOLATION", "1.0"))
W_LATENCY_IMPACT = float(os.environ.get("W_LATENCY_IMPACT", "0.5"))
W_RESOURCE_OVERHEAD = float(os.environ.get("W_RESOURCE_OVERHEAD", "0.2"))
W_FAILURE_PENALTY = float(os.environ.get("W_FAILURE_PENALTY", "5.0"))


def _extract_first_json_object(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object start found")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError("Unterminated JSON object")


def _parse_k6_stdout_summary(stdout_path: Path) -> dict:
    """Parse a k6 stdout file that contains a JSON summary export.

    We expect k6 ASCII art + text + a JSON object containing at least:
      - state.testRunDurationMs
      - metrics.{inference_latency,http_req_failed,checks}
      - root_group.checks (passes/fails)
    """

    text = stdout_path.read_text(errors="ignore")
    summary = _extract_first_json_object(text)
    return summary


def _get_trial_index_by_episode_name(episodes_dir: Path, episode_file_name: str) -> int | None:
    eps = sorted(episodes_dir.glob("episode_*.json"), key=lambda p: p.name)
    for idx, p in enumerate(eps, start=1):
        if p.name == episode_file_name:
            return idx
    return None


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_outcome_from_k6(
    *,
    episode_path: Path,
    decision_snapshot: dict,
    chosen_action: str,
    slo_p95_latency_ms: float = DEFAULT_SLO_P95_LATENCY_MS,
    slo_error_rate: float = DEFAULT_SLO_ERROR_RATE,
) -> dict | None:
    """Best-effort outcome derivation for GKE runs.

    Episode records produced by the controller may have `outcome: null`. However,
    the experiment harness captures per-trial k6 stdout summaries, which include
    aggregated latency and check failure stats. This function maps an episode to
    its trial index and derives:
      - p95 latency (ms)
      - error rate (fraction)
      - duration (seconds)
      - approximate replica_seconds (replicas * seconds)
      - computed_cost (composite)
    """

    try:
        resolved = episode_path.resolve()
    except OSError:
        resolved = episode_path

    episodes_dir = resolved.parent
    combo_dir = episodes_dir.parent
    trial_idx = _get_trial_index_by_episode_name(episodes_dir, resolved.name)
    if trial_idx is None:
        return None

    k6_stdout = combo_dir / "k6_results" / f"trial_{trial_idx}_stdout.txt"
    k6_summary = combo_dir / "k6_results" / f"trial_{trial_idx}_summary.json"

    # Prefer the dedicated summary JSON (from --summary-export) over parsing stdout.
    summary = None
    if k6_summary.exists():
        try:
            summary = json.loads(k6_summary.read_text(errors="ignore"))
        except Exception as e:
            logger.warning("Failed to parse k6 summary JSON %s: %s", k6_summary, e)

    if summary is None and k6_stdout.exists():
        try:
            summary = _parse_k6_stdout_summary(k6_stdout)
        except Exception as e:
            logger.warning("Failed to parse k6 summary %s: %s", k6_stdout, e)

    if summary is None:
        return None

    duration_ms = summary.get("state", {}).get("testRunDurationMs")
    duration_seconds = (_safe_float(duration_ms) or 0.0) / 1000.0

    metrics = summary.get("metrics", {}) or {}

    # Fallback: derive duration from http_reqs count / rate.
    if duration_seconds == 0.0:
        reqs = metrics.get("http_reqs") or {}
        reqs_vals = reqs.get("values", reqs) if isinstance(reqs, dict) else {}
        req_count = _safe_float(reqs_vals.get("count"))
        req_rate = _safe_float(reqs_vals.get("rate"))
        if req_count and req_rate and req_rate > 0:
            duration_seconds = req_count / req_rate

    # k6 --summary-export puts metric values directly under the metric key,
    # whereas handleSummary() wraps them in a "values" sub-object.
    inf_raw = metrics.get("inference_latency") or {}
    inf = inf_raw.get("values", inf_raw) if isinstance(inf_raw, dict) else {}
    p95 = _safe_float(inf.get("p(95)"))
    if p95 is None:
        return None

    # root_group.checks may be a list (handleSummary) or dict keyed by name (--summary-export).
    root_checks_raw = (summary.get("root_group", {}) or {}).get("checks", []) or []
    if isinstance(root_checks_raw, dict):
        root_checks = list(root_checks_raw.values())
    else:
        root_checks = root_checks_raw

    error_rate = None
    for chk in root_checks:
        if chk.get("name") == "status is 200":
            passes = _safe_float(chk.get("passes")) or 0.0
            fails = _safe_float(chk.get("fails")) or 0.0
            denom = passes + fails
            if denom > 0:
                error_rate = fails / denom
            break
    if error_rate is None:
        hrf_raw = metrics.get("http_req_failed") or {}
        hrf = hrf_raw.get("values", hrf_raw) if isinstance(hrf_raw, dict) else {}
        error_rate = _safe_float(hrf.get("rate")) or _safe_float(hrf.get("value"))
    if error_rate is None:
        chk_raw = metrics.get("checks") or {}
        chk_vals = chk_raw.get("values", chk_raw) if isinstance(chk_raw, dict) else {}
        pass_rate = _safe_float(chk_vals.get("rate")) or _safe_float(chk_vals.get("value"))
        error_rate = 1.0 - pass_rate if pass_rate is not None else 0.0

    slo_breach = bool((p95 > slo_p95_latency_ms) or (error_rate > slo_error_rate))

    hpa_current = _safe_float(decision_snapshot.get("hpa_current_replicas"))
    hpa_desired = _safe_float(decision_snapshot.get("hpa_desired_replicas"))
    target = _safe_float(decision_snapshot.get("target_replicas"))

    baseline_replicas = hpa_current or hpa_desired or target or 1.0
    chosen_replicas = target or baseline_replicas
    avg_replicas = max(baseline_replicas, chosen_replicas)

    replica_seconds = avg_replicas * duration_seconds
    resource_overhead_ratio = max(avg_replicas - baseline_replicas, 0.0) / max(baseline_replicas, 1.0)

    latency_norm = np.clip((p95 / slo_p95_latency_ms - 1.0), 0, 10) / 10.0
    resource_norm = np.clip(resource_overhead_ratio, 0, 5) / 5.0
    failure = 0.0
    if chosen_action in {"rollback", "failed"}:
        failure = 1.0

    computed_cost = (
        W_SLO_VIOLATION * (1.0 if slo_breach else 0.0)
        + W_LATENCY_IMPACT * float(latency_norm)
        + W_RESOURCE_OVERHEAD * float(resource_norm)
        + W_FAILURE_PENALTY * failure
    )

    return {
        "slo_violation_seconds": duration_seconds if slo_breach else 0.0,
        "p95_impact": max(p95 - slo_p95_latency_ms, 0.0),
        "p99_impact": 0.0,
        "error_rate_peak": float(error_rate),
        "replica_seconds": float(replica_seconds),
        "success": not slo_breach,
        "rollback": False,
        "duration_seconds": float(duration_seconds),
        "time_to_recovery": 0.0,
        "computed_cost": float(computed_cost),
    }


def load_episode_logs(episode_dir: str) -> pd.DataFrame:
    """Load all episode JSON records into a DataFrame."""
    records = []
    for path in Path(episode_dir).rglob("episode_*.json"):
        with open(path) as f:
            rec = json.load(f)
        # Flatten nested fields
        flat = {
            "run_id": rec.get("run_id"),
            "timestamp": rec.get("timestamp"),
            "policy_version": rec.get("policy_version"),
            "stress_score": rec.get("stress_score"),
            "chosen_action": rec.get("chosen_action"),
            "original_action": rec.get("original_action"),
            "guardrail_override": rec.get("guardrail_override"),
        }
        # Snapshot fields
        snap = rec.get("decision_snapshot", {})
        for k, v in snap.items():
            flat[f"snap_{k}"] = v
        # Outcome fields
        outcome = rec.get("outcome")
        if isinstance(outcome, dict) and outcome:
            for k, v in outcome.items():
                flat[f"outcome_{k}"] = v
        else:
            derived = _derive_outcome_from_k6(
                episode_path=Path(path),
                decision_snapshot=snap,
                chosen_action=flat.get("chosen_action") or "",
            )
            if derived:
                for k, v in derived.items():
                    flat[f"outcome_{k}"] = v
        records.append(flat)

    return pd.DataFrame(records)


def bootstrap_ci(
    data: np.ndarray, stat_fn=np.mean, n_boot: int = 10000, ci: float = 0.95, seed: int = 42
) -> tuple[float, float, float]:
    """Compute bootstrap confidence interval for a statistic."""
    rng = np.random.default_rng(seed)
    boot_stats = []
    for _ in range(n_boot):
        sample = rng.choice(data, size=len(data), replace=True)
        boot_stats.append(stat_fn(sample))
    boot_stats = np.array(boot_stats)

    alpha = (1 - ci) / 2
    lower = np.percentile(boot_stats, alpha * 100)
    upper = np.percentile(boot_stats, (1 - alpha) * 100)
    point = stat_fn(data)

    return point, lower, upper


def mann_whitney_test(group_a: np.ndarray, group_b: np.ndarray) -> dict:
    """Perform Mann-Whitney U test between two groups."""
    if len(group_a) < 1 or len(group_b) < 1:
        return {
            "U_statistic": 0.0,
            "p_value": 1.0,
            "effect_size": 0.0,
            "significant_005": False,
            "significant_001": False,
            "n_a": len(group_a),
            "n_b": len(group_b),
        }

    statistic, p_value = stats.mannwhitneyu(group_a, group_b, alternative="two-sided")

    # Effect size (rank-biserial correlation)
    n1, n2 = len(group_a), len(group_b)
    effect_size = 1 - (2 * statistic) / (n1 * n2)

    return {
        "U_statistic": float(statistic),
        "p_value": float(p_value),
        "effect_size": float(effect_size),
        "significant_005": bool(p_value < 0.05),
        "significant_001": bool(p_value < 0.01),
        "n_a": n1,
        "n_b": n2,
    }


def compare_policies(df: pd.DataFrame, metric: str = "outcome_computed_cost") -> dict:
    """Compare all policies pairwise using Mann-Whitney U tests."""
    if metric not in df.columns:
        logger.warning("Metric %r not present; skipping pairwise comparisons", metric)
        return {}

    policies = df["policy_version"].unique()
    comparisons = {}

    for i, p1 in enumerate(policies):
        for p2 in policies[i + 1 :]:
            data_a = df[df["policy_version"] == p1][metric].dropna().values
            data_b = df[df["policy_version"] == p2][metric].dropna().values

            if len(data_a) < 3 or len(data_b) < 3:
                continue

            test = mann_whitney_test(data_a, data_b)
            key = f"{p1} vs {p2}"
            comparisons[key] = test
            comparisons[key]["mean_a"] = float(np.mean(data_a))
            comparisons[key]["mean_b"] = float(np.mean(data_b))

    return comparisons


def compute_policy_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Compute summary statistics per policy."""
    metrics = [
        "outcome_computed_cost",
        "outcome_slo_violation_seconds",
        "outcome_p95_impact",
        "outcome_replica_seconds",
        "outcome_duration_seconds",
    ]

    rows = []
    for policy in df["policy_version"].unique():
        policy_df = df[df["policy_version"] == policy]
        row = {"policy": policy, "n_episodes": len(policy_df)}

        for metric in metrics:
            if metric in policy_df.columns:
                data = policy_df[metric].dropna().values
                if len(data) > 0:
                    point, ci_lo, ci_hi = bootstrap_ci(data)
                    row[f"{metric}_mean"] = point
                    row[f"{metric}_ci_lo"] = ci_lo
                    row[f"{metric}_ci_hi"] = ci_hi

        # Success rate
        if "outcome_success" in policy_df.columns:
            success = policy_df["outcome_success"].dropna().values
            row["success_rate"] = float(np.mean(success))

        # Action distribution
        action_counts = policy_df["chosen_action"].value_counts(normalize=True)
        for action in ["delay", "pre-scale", "canary", "rolling"]:
            row[f"action_pct_{action}"] = float(action_counts.get(action, 0))

        rows.append(row)

    return pd.DataFrame(rows)


def ablation_analysis(df: pd.DataFrame) -> dict:
    """Perform ablation analysis: what happens when features/actions are removed."""
    results = {}

    # Action ablation: compare with restricted action sets
    for removed_action in ["delay", "pre-scale", "canary", "rolling"]:
        with_action = df[df["chosen_action"] != removed_action]
        without_metric = "outcome_computed_cost"
        if without_metric in df.columns:
            full_costs = df[without_metric].dropna().values
            ablated_costs = with_action[without_metric].dropna().values
            if len(full_costs) > 3 and len(ablated_costs) > 3:
                test = mann_whitney_test(full_costs, ablated_costs)
                results[f"remove_{removed_action}"] = test

    return results


def generate_report(episode_dir: str, output_dir: str):
    """Generate full evaluation report."""
    os.makedirs(output_dir, exist_ok=True)

    logger.info("Loading episode logs...")
    df = load_episode_logs(episode_dir)
    logger.info("Loaded %d episodes", len(df))

    if len(df) == 0:
        logger.warning("No episodes found. Exiting.")
        return

    # Policy summary
    logger.info("Computing policy summaries...")
    summary_df = compute_policy_summary(df)
    summary_df.to_csv(os.path.join(output_dir, "policy_summary.csv"), index=False)
    logger.info("Policy summary:\n%s", summary_df.to_string())

    # Pairwise comparisons (only when we have outcome metrics)
    logger.info("Pairwise comparisons (Mann-Whitney U)...")
    comparisons = compare_policies(df)
    with open(os.path.join(output_dir, "pairwise_comparisons.json"), "w") as f:
        json.dump(comparisons, f, indent=2)

    for pair, result in comparisons.items():
        sig = "***" if result["significant_001"] else ("*" if result["significant_005"] else "ns")
        logger.info(
            "  %s: p=%.4f %s (mean_a=%.4f, mean_b=%.4f)",
            pair,
            result["p_value"],
            sig,
            result["mean_a"],
            result["mean_b"],
        )

    # Ablation analysis (only meaningful when outcome metrics exist)
    logger.info("Ablation analysis...")
    ablation = ablation_analysis(df)
    with open(os.path.join(output_dir, "ablation_results.json"), "w") as f:
        json.dump(ablation, f, indent=2)

    for removed, result in ablation.items():
        sig = "***" if result["significant_001"] else ("*" if result["significant_005"] else "ns")
        logger.info("  %s: p=%.4f %s", removed, result["p_value"], sig)

    # Scenario-level breakdown
    logger.info("Scenario breakdown...")
    if "snap_rps" in df.columns and "outcome_computed_cost" in df.columns:
        scenario_summary = df.groupby(["chosen_action"]).agg(
            {
                "outcome_computed_cost": ["mean", "std", "count"],
                "stress_score": "mean",
            }
        )
        scenario_summary.to_csv(os.path.join(output_dir, "scenario_breakdown.csv"))
        logger.info("Scenario breakdown:\n%s", scenario_summary.to_string())

    logger.info("Report saved to %s/", output_dir)


if __name__ == "__main__":
    episode_dir = sys.argv[1] if len(sys.argv) > 1 else "experiments/episodes"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "experiments/reports"
    generate_report(episode_dir, output_dir)

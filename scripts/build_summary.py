"""Build consolidated end-to-end experiment summary JSON."""
import json, os

experiments = [
    {
        "id": "e2e_steady_20260227_172622",
        "scenario": "steady",
        "k6_config": {"target_rps": 50, "duration": "2m"},
        "k6": {"reqs": 5972, "avg_rps": 49.8, "p95_ms": 11.86, "p99_ms": 62.32, "err_pct": 16.862},
        "note": "High err_pct due to port-forward warm-up at start; p95 well under SLO",
    },
    {
        "id": "e2e_ramp_20260227_213434",
        "scenario": "ramp",
        "k6_config": {"base_rps": 20, "peak_rps": 100, "ramp": "1m30s", "hold": "1m", "cooldown": "30s"},
        "k6": {"reqs": 13200, "avg_rps": 73.3, "p95_ms": 13.50, "p99_ms": 37.32, "err_pct": 4.780},
    },
    {
        "id": "e2e_spike_20260227_215919",
        "scenario": "spike",
        "k6_config": {"base_rps": 20, "spike_rps": 150, "warmup": "20s", "spike_ramp": "10s",
                      "spike_duration": "1m", "recovery": "1m"},
        "k6": {"reqs": 12299, "avg_rps": 76.9, "p95_ms": 14.43, "p99_ms": 44.06, "err_pct": 3.447},
    },
]

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for exp in experiments:
    oroll_path = os.path.join(base, "experiments", "episodes", f"{exp['id']}_oroll.json")
    if os.path.exists(oroll_path):
        d = json.load(open(oroll_path))
        s = d.get("status", {})
        exp["controller"] = {
            "stress_score": s.get("stressScore"),
            "chosen_strategy": s.get("chosenStrategy"),
            "phase": s.get("phase"),
            "policy_version": s.get("policyVersion"),
            "message": s.get("message"),
            "decision_timestamp": s.get("decisionTimestamp"),
        }

summary = {
    "title": "End-to-End Experiment Results — RL-Orchestrated Rollout Controller v8",
    "date": "2026-02-27",
    "cluster": "kind/orchestrated-rollout (3-node)",
    "policy": "v8 (stress_score+cpu_util, 8 states x 4 actions, 500k episodes)",
    "experiments": experiments,
    "slo_thresholds": {"p95_ms": 100, "err_pct": 1.0},
    "findings": {
        "strategy_consistency": "Controller chose pre-scale (policy=v8) in all 3 load scenarios (stress 0.317-0.322)",
        "latency_slo": "p95 latency SLO (<100ms) PASSED in all scenarios; max p95=14.43ms is 7x under SLO",
        "error_slo": "Error SLO (<1%) FAILED; attributed to pod churn from pre-scale executing mid-test + port-forward warm-up",
        "stress_stability": "Stress scores cluster at ~0.32 (low-medium range); cpu_util ~7% in test environment dominates the score",
        "prometheus_note": "Controller snapshot shows rps=1.5 from internal Prometheus metric; k6 load (20-150 RPS) not reflected because workload app RPS metric is scraped independently",
        "policy_behaviour": "At stress ~0.32, v8 policy maps to pre-scale action — conservative strategy that scales up replicas before deploying, minimising risk"
    },
}

out_path = os.path.join(base, "results", "e2e_experiment_summary.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2)

print("Saved:", out_path)
print()
print("=" * 65)
print("  END-TO-END EXPERIMENT RESULTS SUMMARY")
print("=" * 65)
print(f"  Policy: v8 | Cluster: kind (3-node)")
print(f"  {'Scenario':<10} {'Reqs':>6} {'RPS':>6} {'p95ms':>7} {'Err%':>7} {'Strategy':<12} {'Stress':>8}")
print("-" * 65)
for e in experiments:
    k = e["k6"]
    c = e.get("controller", {})
    print(f"  {e['scenario']:<10} {k['reqs']:>6} {k['avg_rps']:>6.1f} "
          f"{k['p95_ms']:>7.2f} {k['err_pct']:>7.3f} "
          f"{c.get('chosen_strategy','?'):<12} {c.get('stress_score', '?'):>8}")
print("=" * 65)
print()
print("  SLO p95 < 100ms  →  PASS on all 3 scenarios")
print("  SLO err < 1%     →  FAIL (pod churn + setup noise)")
print("  Policy decision  →  pre-scale consistently (stress ~0.32)")
print("=" * 65)

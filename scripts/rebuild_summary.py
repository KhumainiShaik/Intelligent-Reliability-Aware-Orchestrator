#!/usr/bin/env python3
"""Rebuild comparison summary from k6_summary.json + oroll.json files."""
import json, os, sys

out_dir = sys.argv[1] if len(sys.argv) > 1 else "experiments/comparison_20260228_155555"
runs = ["rl-adaptive", "fixed-rolling", "fixed-canary", "fixed-prescale"]

print(f"\n{'Run':<18} {'Strategy':<12} {'Stress':>7} {'Reqs':>7} {'RPS':>6} {'p95ms':>7} {'p99ms':>7} {'Err%':>7}")
print("-" * 82)

results = []
for run in runs:
    run_dir = os.path.join(out_dir, run)

    # OrchestratedRollout status
    oroll_path = os.path.join(run_dir, "oroll.json")
    strategy, stress_val, policy = "?", "?", "?"
    if os.path.exists(oroll_path):
        try:
            d = json.load(open(oroll_path))
            s = d.get("status", {})
            strategy = s.get("chosenStrategy", "?")
            stress_val = f"{s.get('stressScore', 0):.3f}"
            policy = s.get("policyVersion", "?")
        except Exception:
            pass

    # k6 summary (direct format)
    summary_path = os.path.join(run_dir, "k6_summary.json")
    reqs, rps, p95, p99, err_pct = 0, 0.0, 0.0, 0.0, 0.0
    if os.path.exists(summary_path):
        try:
            sm = json.load(open(summary_path))
            m = sm.get("metrics", {})
            reqs = int(m.get("http_reqs", {}).get("count", 0))
            rps = m.get("http_reqs", {}).get("rate", 0.0)
            p95 = m.get("inference_latency", {}).get("p(95)", 0.0)
            p99 = m.get("inference_latency", {}).get("p(99)", 0.0)
            err_count = m.get("error_rate", {}).get("passes", 0)
            err_pct = err_count / reqs * 100 if reqs > 0 else 0.0
        except Exception:
            pass

    print(f"{run:<18} {strategy:<12} {stress_val:>7} {reqs:>7} {rps:>6.1f} {p95:>7.1f} {p99:>7.1f} {err_pct:>7.3f}")
    results.append({
        "run": run, "strategy": strategy, "stress": stress_val, "policy": policy,
        "reqs": reqs, "rps": round(rps, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
        "err_pct": round(err_pct, 3),
    })

# Save
summary = {"timestamp": os.path.basename(out_dir), "runs": results}
with open(os.path.join(out_dir, "summary.json"), "w") as f:
    json.dump(summary, f, indent=2)
print(f"\nSaved to {out_dir}/summary.json")

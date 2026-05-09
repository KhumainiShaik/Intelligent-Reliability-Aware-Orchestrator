#!/usr/bin/env python3
"""Parse k6 experiment results from stdout JSON files."""

import json
import glob
import sys
import os


def parse_k6_result(stdout_file: str) -> dict:
    """Parse k6 summary JSON from stdout capture file."""
    with open(stdout_file) as f:
        raw = f.read()

    start = raw.find('{')
    end = raw.rfind('}') + 1
    if start < 0 or end <= start:
        return {"error": "No JSON found in file"}

    try:
        d = json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}

    m = d.get("metrics", {})
    dur = m.get("inference_latency", {}).get("values", {})
    errs = m.get("error_rate", {}).get("values", {})
    reqs = m.get("http_reqs", {}).get("values", {})
    thresh_dur = m.get("inference_latency", {}).get("thresholds", {})
    thresh_err = m.get("error_rate", {}).get("thresholds", {})

    # Handle threshold check — value may be bool or dict
    def thresh_ok(t: dict) -> str:
        if not t:
            return "N/A"
        v = list(t.values())[0]
        if isinstance(v, dict):
            return "PASS" if v.get("ok") else "FAIL"
        return "PASS" if v else "FAIL"

    return {
        "file": os.path.basename(stdout_file),
        "requests": reqs.get("count", 0),
        "throughput_rps": round(reqs.get("rate", 0), 2),
        "p95_ms": round(dur.get("p(95)", 0), 2),
        "p99_ms": round(dur.get("p(99)", 0), 2),
        "avg_ms": round(dur.get("avg", 0), 2),
        "error_rate_pct": round(errs.get("rate", 0) * 100, 3),
        "slo_p95": thresh_ok(thresh_dur),
        "slo_error": thresh_ok(thresh_err),
    }


def main():
    # Find all result files (allow passing specific file as arg)
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        files = sorted(glob.glob("experiments/*_k6_stdout.txt"))

    if not files:
        print("No k6 result files found in experiments/")
        sys.exit(1)

    header_printed = False
    for f in files:
        result = parse_k6_result(f)
        if not header_printed:
            print(f"\n{'Scenario/Experiment':<45} {'Reqs':>6} {'RPS':>6} {'p95ms':>7} "
                  f"{'p99ms':>7} {'ErrPct':>8} {'SLO-p95':>8} {'SLO-err':>8}")
            print("-" * 100)
            header_printed = True

        if "error" in result:
            print(f"{result['file']:<45} ERROR: {result['error']}")
        else:
            print(f"{result['file']:<45} {result['requests']:>6} {result['throughput_rps']:>6.1f} "
                  f"{result['p95_ms']:>7.2f} {result['p99_ms']:>7.2f} "
                  f"{result['error_rate_pct']:>8.3f} {result['slo_p95']:>8} {result['slo_error']:>8}")

    print()


if __name__ == "__main__":
    main()

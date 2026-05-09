"""Print concise run metrics for one experiment trial or an aggregate run."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


def _load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _metric(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data.get("metrics", {}).get(name, {})
    if isinstance(raw, dict) and isinstance(raw.get("values"), dict):
        return raw["values"]
    if isinstance(raw, dict):
        return raw
    return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        val = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(val) else val


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.2f} ms"


def _fmt_seconds(value: float) -> str:
    return f"{value:.1f}s"


def _fmt_rate(value: float) -> str:
    return f"{value:.2f} req/s"


def _pct(value: float) -> str:
    return f"{value * 100:.3f}%"


def _latency_summary(values: dict[str, Any]) -> dict[str, float | None]:
    return {
        "avg_ms": _num(values.get("avg")),
        "median_ms": _num(values.get("med")),
        "p90_ms": _num(values.get("p(90)")),
        "p95_ms": _num(values.get("p(95)")),
        "p99_ms": None if values.get("p(99)") is None else _num(values.get("p(99)")),
        "max_ms": _num(values.get("max")),
    }


def summarise_trial(args: argparse.Namespace) -> dict[str, Any]:
    oroll = _load_json(args.oroll)
    k6 = _load_json(args.k6_summary)

    status = oroll.get("status", {})
    release = oroll.get("spec", {}).get("release", {})

    http_reqs = _metric(k6, "http_reqs")
    iterations = _metric(k6, "iterations")
    http_failed = _metric(k6, "http_req_failed")
    custom_error_rate = _metric(k6, "error_rate")
    checks = _metric(k6, "checks")

    request_count = int(_num(http_reqs.get("count")))
    failed_requests = int(_num(http_failed.get("passes")))
    http_error_rate = _num(http_failed.get("rate", http_failed.get("value")))
    custom_error_rate_value = _num(custom_error_rate.get("rate", custom_error_rate.get("value")))
    check_passes = int(_num(checks.get("passes")))
    check_fails = int(_num(checks.get("fails")))
    check_total = check_passes + check_fails
    check_rate = (check_passes / check_total) if check_total else 0.0

    metrics: dict[str, Any] = {
        "trial": args.trial,
        "scenario": args.scenario,
        "fault": args.fault,
        "chaos_result_name": args.chaos_result_name,
        "chaos_phase": args.chaos_phase,
        "chaos_verdict": args.chaos_verdict,
        "phase": status.get("phase", "Unknown"),
        "strategy": status.get("chosenStrategy", "unknown"),
        "policy_version": status.get("policyVersion", "unknown"),
        "stress_score": _num(status.get("stressScore")),
        "pre_scale_extra_replicas": status.get("preScaleExtraReplicas"),
        "message": status.get("message", ""),
        "release_image": release.get("image", ""),
        "release_tag": release.get("tag", ""),
        "reset_seconds": _num(args.reset_seconds),
        "model_load_seconds": args.model_load_seconds,
        "rollout_seconds": _num(args.rollout_seconds),
        "k6_wall_seconds": _num(args.k6_wall_seconds),
        "trial_seconds": _num(args.trial_seconds),
        "request_count": request_count,
        "throughput_rps": _num(http_reqs.get("rate")),
        "iteration_rate": _num(iterations.get("rate")),
        "http_error_rate": http_error_rate,
        "custom_error_rate": custom_error_rate_value,
        "failed_requests": failed_requests,
        "check_passes": check_passes,
        "check_fails": check_fails,
        "check_rate": check_rate,
        "http_latency": _latency_summary(_metric(k6, "http_req_duration")),
        "inference_latency": _latency_summary(_metric(k6, "inference_latency")),
    }

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("")
    print("==============================================")
    print(f"  Trial {args.trial} Metrics")
    print("==============================================")
    print(
        "  Result: "
        f"{metrics['phase']} | strategy={metrics['strategy']} | "
        f"policy={metrics['policy_version']} | stress={metrics['stress_score']:.3f}"
    )
    if metrics["pre_scale_extra_replicas"] is not None:
        print(f"  Pre-scale extra:    +{metrics['pre_scale_extra_replicas']} replicas")
    print(f"  Release: {metrics['release_image']}:{metrics['release_tag']}")
    print(f"  Workload reset time: {_fmt_seconds(metrics['reset_seconds'])}")
    if metrics["model_load_seconds"] is not None:
        print(f"  Model load time:     {_fmt_seconds(metrics['model_load_seconds'])}")
    print(f"  Rollout/deploy time: {_fmt_seconds(metrics['rollout_seconds'])}")
    print(f"  k6 load-test time:   {_fmt_seconds(metrics['k6_wall_seconds'])}")
    print(f"  Total trial time:    {_fmt_seconds(metrics['trial_seconds'])}")
    if metrics["chaos_phase"] or metrics["chaos_verdict"]:
        print(
            "  Fault injection:     "
            f"{metrics['chaos_phase'] or 'unknown'} / {metrics['chaos_verdict'] or 'unknown'}"
        )
    print(f"  Throughput:          {_fmt_rate(metrics['throughput_rps'])} ({request_count:,} requests)")
    print(
        "  HTTP latency:        "
        f"avg={_fmt_ms(metrics['http_latency']['avg_ms'])}, "
        f"p50={_fmt_ms(metrics['http_latency']['median_ms'])}, "
        f"p95={_fmt_ms(metrics['http_latency']['p95_ms'])}, "
        f"p99={_fmt_ms(metrics['http_latency']['p99_ms'])}, "
        f"max={_fmt_ms(metrics['http_latency']['max_ms'])}"
    )
    print(
        "  Inference latency:   "
        f"avg={_fmt_ms(metrics['inference_latency']['avg_ms'])}, "
        f"p95={_fmt_ms(metrics['inference_latency']['p95_ms'])}, "
        f"p99={_fmt_ms(metrics['inference_latency']['p99_ms'])}"
    )
    print(
        "  Errors/checks:       "
        f"http_error={_pct(metrics['http_error_rate'])}, "
        f"custom_error={_pct(metrics['custom_error_rate'])}, "
        f"failed_requests={failed_requests:,}, "
        f"checks={check_passes:,} pass / {check_fails:,} fail ({_pct(check_rate)})"
    )
    if metrics["message"]:
        print(f"  Controller message:  {metrics['message']}")
    print("==============================================")

    return metrics


def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    trials = [_load_json(path) for path in args.aggregate]
    if not trials:
        raise SystemExit("No trial metrics files supplied for aggregation")

    phases = Counter(str(item.get("phase", "Unknown")) for item in trials)
    strategies = Counter(str(item.get("strategy", "unknown")) for item in trials)
    pre_scale_extra = Counter(
        str(item.get("pre_scale_extra_replicas"))
        for item in trials
        if item.get("pre_scale_extra_replicas") is not None
    )
    chaos_verdicts = Counter(
        str(item.get("chaos_verdict", "unknown")) for item in trials if item.get("chaos_verdict")
    )
    total_requests = sum(int(_num(item.get("request_count"))) for item in trials)
    total_failed = sum(int(_num(item.get("failed_requests"))) for item in trials)

    def avg_field(name: str) -> float:
        return mean(_num(item.get(name)) for item in trials)

    def avg_optional_field(name: str) -> float | None:
        values = [_num(value) for item in trials if (value := item.get(name)) is not None]
        return mean(values) if values else None

    def avg_latency(path: str) -> float | None:
        family, metric = path.split(".")
        values = [
            _num(value)
            for item in trials
            if (value := item.get(family, {}).get(metric)) is not None
        ]
        return mean(values) if values else None

    result: dict[str, Any] = {
        "trials": len(trials),
        "phases": dict(phases),
        "strategies": dict(strategies),
        "pre_scale_extra_replicas": dict(pre_scale_extra),
        "chaos_verdicts": dict(chaos_verdicts),
        "total_requests": total_requests,
        "total_failed_requests": total_failed,
        "mean_throughput_rps": avg_field("throughput_rps"),
        "mean_rollout_seconds": avg_field("rollout_seconds"),
        "mean_model_load_seconds": avg_optional_field("model_load_seconds"),
        "mean_trial_seconds": avg_field("trial_seconds"),
        "mean_http_p95_ms": avg_latency("http_latency.p95_ms"),
        "mean_http_p99_ms": avg_latency("http_latency.p99_ms"),
        "mean_inference_p95_ms": avg_latency("inference_latency.p95_ms"),
        "mean_error_rate": mean(_num(item.get("http_error_rate")) for item in trials),
    }

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("")
    print("==============================================")
    print("  Run Metrics Summary")
    print("==============================================")
    print(f"  Trials:              {result['trials']}")
    print(f"  Phases:              {', '.join(f'{k}={v}' for k, v in sorted(phases.items()))}")
    print(f"  Strategies:          {', '.join(f'{k}={v}' for k, v in sorted(strategies.items()))}")
    if pre_scale_extra:
        print(
            "  Pre-scale extra:     "
            f"{', '.join(f'+{k}={v}' for k, v in sorted(pre_scale_extra.items()))}"
        )
    if chaos_verdicts:
        print(f"  Chaos verdicts:      {', '.join(f'{k}={v}' for k, v in sorted(chaos_verdicts.items()))}")
    print(f"  Total requests:      {total_requests:,}")
    print(f"  Failed requests:     {total_failed:,}")
    print(f"  Mean throughput:     {_fmt_rate(result['mean_throughput_rps'])}")
    if result["mean_model_load_seconds"] is not None:
        print(f"  Mean model load:     {_fmt_seconds(result['mean_model_load_seconds'])}")
    print(f"  Mean rollout time:   {_fmt_seconds(result['mean_rollout_seconds'])}")
    print(f"  Mean trial time:     {_fmt_seconds(result['mean_trial_seconds'])}")
    print(f"  Mean HTTP P95:       {_fmt_ms(result['mean_http_p95_ms'])}")
    print(f"  Mean HTTP P99:       {_fmt_ms(result['mean_http_p99_ms'])}")
    print(f"  Mean inference P95:  {_fmt_ms(result['mean_inference_p95_ms'])}")
    print(f"  Mean error rate:     {_pct(result['mean_error_rate'])}")
    print("==============================================")

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=int)
    parser.add_argument("--scenario", default="unknown")
    parser.add_argument("--fault", default="unknown")
    parser.add_argument("--chaos-result-name", default="")
    parser.add_argument("--chaos-phase", default="")
    parser.add_argument("--chaos-verdict", default="")
    parser.add_argument("--oroll")
    parser.add_argument("--k6-summary")
    parser.add_argument("--output")
    parser.add_argument("--reset-seconds", type=float, default=0.0)
    parser.add_argument("--model-load-seconds", type=float)
    parser.add_argument("--rollout-seconds", type=float, default=0.0)
    parser.add_argument("--k6-wall-seconds", type=float, default=0.0)
    parser.add_argument("--trial-seconds", type=float, default=0.0)
    parser.add_argument("--aggregate", nargs="*")
    args = parser.parse_args()

    if args.aggregate is not None:
        return args
    if args.trial is None or not args.oroll or not args.k6_summary:
        parser.error("--trial, --oroll and --k6-summary are required unless --aggregate is used")
    return args


def main() -> None:
    args = parse_args()
    if args.aggregate is not None:
        aggregate(args)
    else:
        summarise_trial(args)


if __name__ == "__main__":
    main()

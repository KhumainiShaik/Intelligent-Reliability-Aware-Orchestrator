#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure repo root is importable when running as scripts/demo_policy_decision.py
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from controller.guardrails import Guardrails  # noqa: E402
from controller.policy import Engine  # noqa: E402
from controller.stress import Calculator  # noqa: E402


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class Snapshot:
    # Service health
    rps: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0

    # Cluster pressure
    pending_pods: int = 0
    node_cpu_util: float = 0.0
    node_mem_util: float = 0.0

    # Autoscaling context
    hpa_desired_replicas: int = 0
    hpa_current_replicas: int = 0

    # Forecast/trend
    stress_forecast: float = 0.0

    # Degraded snapshot flag
    degraded: bool = False


def _stress_breakdown(snap: Snapshot) -> dict[str, float]:
    # Defaults match controller.stress.Calculator
    weight_latency = 0.25
    weight_error_rate = 0.25
    weight_pending_pod = 0.15
    weight_cpu = 0.15
    weight_mem = 0.10
    weight_hpa_gap = 0.10

    latency_ceiling_ms = 500.0
    error_rate_ceiling = 0.05
    pending_pods_ceiling = 10.0

    latency_stress = _clamp((snap.p95_latency_ms or 0.0) / latency_ceiling_ms)

    if snap.error_rate <= 0:
        error_stress = 0.0
    else:
        error_stress = _clamp(math.sqrt(snap.error_rate / error_rate_ceiling))

    pending_stress = _clamp(float(max(snap.pending_pods, 0)) / pending_pods_ceiling)
    cpu_stress = _clamp(snap.node_cpu_util)
    mem_stress = _clamp(snap.node_mem_util)

    if snap.hpa_current_replicas <= 0 or snap.hpa_desired_replicas <= snap.hpa_current_replicas:
        hpa_gap_stress = 0.0
    else:
        hpa_gap_stress = _clamp(
            (snap.hpa_desired_replicas - snap.hpa_current_replicas)
            / max(snap.hpa_current_replicas, 1)
        )

    base = (
        weight_latency * latency_stress
        + weight_error_rate * error_stress
        + weight_pending_pod * pending_stress
        + weight_cpu * cpu_stress
        + weight_mem * mem_stress
        + weight_hpa_gap * hpa_gap_stress
    )

    return {
        "latency_sub": latency_stress,
        "error_sub": error_stress,
        "pending_sub": pending_stress,
        "cpu_sub": cpu_stress,
        "mem_sub": mem_stress,
        "hpa_gap_sub": hpa_gap_stress,
        "stress_base": _clamp(base),
    }


def _print_case(
    title: str,
    snap: Snapshot,
    engine: Engine,
    slo_p95_ms: float,
    slo_error_rate: float,
) -> None:
    print(f"\n== {title} ==")

    p95_breach = snap.p95_latency_ms > slo_p95_ms
    err_breach = snap.error_rate > slo_error_rate

    print(
        "snapshot: "
        f"rps={snap.rps:.1f}  p95={snap.p95_latency_ms:.1f}ms ({'BREACH' if p95_breach else 'ok'})  "
        f"err={snap.error_rate:.4f} ({'BREACH' if err_breach else 'ok'})"
    )
    print(
        "cluster:  "
        f"pending={snap.pending_pods}  cpu={snap.node_cpu_util:.2f}  mem={snap.node_mem_util:.2f}  "
        f"hpa={snap.hpa_current_replicas}->{snap.hpa_desired_replicas}  forecast={snap.stress_forecast:.2f}  "
        f"degraded={snap.degraded}"
    )

    # New calculator per case so EWMA state doesn't carry between cases.
    stress_calc = Calculator()
    stress_score = stress_calc.compute(snap)

    b = _stress_breakdown(snap)
    print(
        "stress:   "
        f"score={stress_score:.3f}  "
        f"(lat={b['latency_sub']:.2f} err={b['error_sub']:.2f} pend={b['pending_sub']:.2f} "
        f"cpu={b['cpu_sub']:.2f} mem={b['mem_sub']:.2f} hpaGap={b['hpa_gap_sub']:.2f})"
    )

    # Policy lookup
    action, ver = engine.select_action(snap, stress_score)

    # Explain discretised state + Q-values
    try:
        state_key = engine._discretise(snap, stress_score)  # demo helper
        feat_names = [f.name for f in engine._artifact.features]  # demo helper
        bins = state_key.split("_")
        print(f"state:    key={state_key}  bins={{" + ", ".join(f"{n}={b}" for n, b in zip(feat_names, bins)) + "}")

        q_values = engine._artifact.q_table.get(state_key, [])
        actions = engine._artifact.actions
        if q_values and actions:
            print("q_values:")
            for a, q in zip(actions, q_values):
                marker = "<-- chosen" if a == action else ""
                print(f"  - {a:9s}  Q={q:8.3f}  {marker}")
    except Exception:
        # Keep demo resilient even if internals change
        pass

    # Guardrails
    guard = Guardrails()
    final_action, overridden, reason = guard.apply(action, snap, stress_score, config=None)

    if overridden:
        print(f"decision: policy={ver}  action={action}  → final={final_action}  (OVERRIDDEN: {reason})")
    else:
        print(f"decision: policy={ver}  action={action}  → final={final_action}")


def main() -> int:
    default_policy_dir = REPO_ROOT / "artifacts" / "v11_no_forecast"

    ap = argparse.ArgumentParser(
        description="Offline demo: snapshot → stress score → Q-table lookup → guardrails (with rich output)."
    )
    ap.add_argument(
        "--policy-dir",
        type=Path,
        default=default_policy_dir,
        help=f"Directory containing policy_artifact.json (default: {default_policy_dir})",
    )
    ap.add_argument(
        "--slo-p95-ms",
        type=float,
        default=100.0,
        help="SLO threshold for P95 latency (ms) to label BREACH/ok in output.",
    )
    ap.add_argument(
        "--slo-error-rate",
        type=float,
        default=0.01,
        help="SLO threshold for error rate to label BREACH/ok in output.",
    )
    args = ap.parse_args()

    policy_dir: Path = args.policy_dir
    if not (policy_dir / "policy_artifact.json").exists():
        print(f"ERROR: policy_artifact.json not found under: {policy_dir}", file=sys.stderr)
        return 2

    engine = Engine(str(policy_dir))

    print("Offline policy decision demo")
    print(f"policy_dir: {policy_dir}")
    print(f"SLO (for labelling): p95<={args.slo_p95_ms}ms, error_rate<={args.slo_error_rate}")

    _print_case(
        "Calm cluster, no spike expected",
        Snapshot(
            rps=5,
            p95_latency_ms=15,
            error_rate=0.0,
            pending_pods=0,
            node_cpu_util=0.25,
            node_mem_util=0.35,
            hpa_desired_replicas=2,
            hpa_current_replicas=2,
            stress_forecast=0.0,
        ),
        engine,
        args.slo_p95_ms,
        args.slo_error_rate,
    )

    _print_case(
        "Calm now, spike forecasted",
        Snapshot(
            rps=50,
            p95_latency_ms=25,
            error_rate=0.0,
            pending_pods=0,
            node_cpu_util=0.30,
            node_mem_util=0.40,
            hpa_desired_replicas=4,
            hpa_current_replicas=2,
            stress_forecast=0.6,
        ),
        engine,
        args.slo_p95_ms,
        args.slo_error_rate,
    )

    _print_case(
        "Higher stress + incoming spike",
        Snapshot(
            rps=300,
            p95_latency_ms=220,
            error_rate=0.02,
            pending_pods=3,
            node_cpu_util=0.80,
            node_mem_util=0.70,
            hpa_desired_replicas=8,
            hpa_current_replicas=4,
            stress_forecast=0.6,
        ),
        engine,
        args.slo_p95_ms,
        args.slo_error_rate,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Ensure repo root is importable when running as scripts/demo_rollout_report.py
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from controller.guardrails import Guardrails  # noqa: E402
from controller.policy import Engine  # noqa: E402
from controller.stress import Calculator  # noqa: E402


class NoData(Exception):
    pass


@dataclass
class Snapshot:
    rps: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    pending_pods: int = 0
    node_cpu_util: float = 0.0
    node_mem_util: float = 0.0
    hpa_desired_replicas: int = 0
    hpa_current_replicas: int = 0
    stress_forecast: float = 0.0
    degraded: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _run_kubectl(args: list[str], timeout_s: int = 30) -> str:
    try:
        p = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("kubectl not found on PATH") from exc

    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {stderr}")

    return p.stdout


def _kubectl_json(args: list[str]) -> dict[str, Any]:
    return json.loads(_run_kubectl(args))


def _pick_latest_oroll(namespace: str) -> str:
    data = _kubectl_json(["get", "oroll", "-n", namespace, "-o", "json"])
    items = data.get("items", []) or []
    if not items:
        raise RuntimeError(f"no OrchestratedRollout resources found in namespace {namespace}")

    items.sort(key=lambda it: it.get("metadata", {}).get("creationTimestamp", ""))
    name = items[-1].get("metadata", {}).get("name")
    if not name:
        raise RuntimeError("failed to determine latest OrchestratedRollout name")
    return str(name)


@contextmanager
def _prometheus_port_forward(prom_namespace: str, prom_service: str, local_port: int):
    cmd = [
        "kubectl",
        "port-forward",
        "-n",
        prom_namespace,
        f"svc/{prom_service}",
        f"{local_port}:9090",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the local port to accept connections.
        for _ in range(40):  # ~20s
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else "").strip()
                raise RuntimeError(f"port-forward exited early: {err}")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("port-forward did not become ready")

        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _prom_query_scalar(base_url: str, promql: str, timeout_s: int = 8) -> float:
    url = f"{base_url}/api/v1/query?query={quote(promql, safe='')}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc

    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: status={payload.get('status')}")

    results = payload.get("data", {}).get("result", [])
    if not results:
        raise NoData("no data")

    value_pair = results[0].get("value", [])
    if len(value_pair) < 2:
        raise RuntimeError("unexpected prometheus value format")

    raw_val = value_pair[1]
    try:
        return float(raw_val)
    except (TypeError, ValueError):
        raise RuntimeError(f"non-numeric prometheus value: {raw_val!r}")


def _query_first(base_url: str, candidates: list[tuple[str, str]]):
    """Try candidates in order; return (value, label, error)."""
    last_err: str | None = None
    for promql, label in candidates:
        try:
            v = _prom_query_scalar(base_url, promql)
            if math.isfinite(v):
                return v, label, None
            last_err = "non-finite value"
        except NoData:
            last_err = "no data"
        except Exception as exc:
            last_err = str(exc)

    return 0.0, "", last_err or "unknown error"


def _pod_is_ready(pod: dict[str, Any]) -> bool:
    for c in pod.get("status", {}).get("conditions", []) or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def main() -> int:
    default_policy_dir = REPO_ROOT / "artifacts" / "v11_no_forecast"

    ap = argparse.ArgumentParser(
        description=(
            "Demo report: show OrchestratedRollout spec/status + current cluster signals "
            "(Prometheus/K8s) + recomputed stress/policy decision."
        )
    )
    ap.add_argument("--namespace", default="orchestrated-rollout", help="OrchestratedRollout namespace")
    ap.add_argument("--oroll", default="", help="OrchestratedRollout name (default: latest in namespace)")
    ap.add_argument("--scenario", default="", help="Optional label to print (e.g. steady/ramp/spike)")
    ap.add_argument("--fault", default="", help="Optional label to print (e.g. none/pod-kill/cpu-stress)")

    ap.add_argument(
        "--policy-dir",
        type=Path,
        default=default_policy_dir,
        help=f"Directory containing policy_artifact.json (default: {default_policy_dir})",
    )

    ap.add_argument("--prom-namespace", default="monitoring", help="Prometheus namespace")
    ap.add_argument("--prom-service", default="prometheus", help="Prometheus service name")
    ap.add_argument("--no-prom", action="store_true", help="Skip Prometheus queries")

    args = ap.parse_args()

    namespace: str = str(args.namespace)
    oroll_name: str = str(args.oroll)

    if not oroll_name:
        oroll_name = _pick_latest_oroll(namespace)

    # --- Kubernetes context ---
    context = _run_kubectl(["config", "current-context"]).strip()
    nodes = _kubectl_json(["get", "nodes", "-o", "json"]).get("items", []) or []
    arches = sorted(
        {
            str(n.get("status", {}).get("nodeInfo", {}).get("architecture", "?"))
            for n in nodes
        }
    )

    # --- OrchestratedRollout ---
    oroll = _kubectl_json(["get", "oroll", oroll_name, "-n", namespace, "-o", "json"])
    spec = oroll.get("spec", {}) or {}
    status = oroll.get("status", {}) or {}

    target = (spec.get("targetRef", {}) or {}).get("name", "")
    release = spec.get("release", {}) or {}
    slo = spec.get("slo", {}) or {}
    action_set = spec.get("actionSet") or []
    rollout_hints = spec.get("rolloutHints", {}) or {}
    guardrail_cfg = spec.get("guardrailConfig", {}) or {}

    print("==============================================")
    print(" Orchestrated Rollout — Demo Report")
    print("==============================================")
    print(f"timestamp_utc:   {_now()}")
    print(f"kubectl_context: {context}")
    print(f"nodes:          count={len(nodes)}  arch={arches}")

    if args.scenario or args.fault:
        print(f"scenario:       {args.scenario or 'n/a'}")
        print(f"fault:          {args.fault or 'n/a'}")

    print("")
    print(f"oroll:          {namespace}/{oroll_name}")
    print(f"target:         deployment/{target}")
    print(f"release:        {release.get('image', '')}:{release.get('tag', '')}")
    print(f"actionSet:      {action_set if action_set else '(default policy actions)'}")
    print(
        "slo:            "
        f"maxP95LatencyMs={slo.get('maxP95LatencyMs', 'n/a')}  "
        f"maxErrorRate={slo.get('maxErrorRate', 'n/a')}"
    )
    if rollout_hints:
        print(f"rolloutHints:   {rollout_hints}")
    if guardrail_cfg:
        print(f"guardrails:     {guardrail_cfg}")

    print("")
    print("status:")
    for k in [
        "phase",
        "chosenStrategy",
        "stressScore",
        "policyVersion",
        "decisionTimestamp",
        "startTimestamp",
        "completionTimestamp",
        "message",
    ]:
        if k in status:
            print(f"  - {k}: {status.get(k)}")

    # --- Workload/HPA quick state (kubectl) ---
    print("")
    print("k8s_state:")
    deploy = {}
    if target:
        try:
            deploy = _kubectl_json(["get", "deploy", target, "-n", namespace, "-o", "json"])
            spec_rep = deploy.get("spec", {}).get("replicas")
            st_rep = deploy.get("status", {}).get("replicas")
            ready_rep = deploy.get("status", {}).get("readyReplicas")
            print(f"  - deployment.replicas: spec={spec_rep}  status={st_rep}  ready={ready_rep}")

            containers = (
                deploy.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
                or []
            )
            if containers:
                c0 = containers[0]
                print(f"  - container.image: {c0.get('image', '')}")
                args_list = c0.get("args")
                if isinstance(args_list, list) and args_list:
                    print(f"  - container.args: {args_list}")
        except Exception as exc:
            print(f"  - deployment: ERROR: {exc}")

    hpa_name = f"{target}-hpa" if target else ""
    if hpa_name:
        try:
            hpa = _kubectl_json(["get", "hpa", hpa_name, "-n", namespace, "-o", "json"])
            hpa_cur = hpa.get("status", {}).get("currentReplicas")
            hpa_des = hpa.get("status", {}).get("desiredReplicas")
            hpa_min = hpa.get("spec", {}).get("minReplicas")
            hpa_max = hpa.get("spec", {}).get("maxReplicas")
            print(f"  - hpa: {hpa_cur}->{hpa_des}  (min={hpa_min}, max={hpa_max})")
        except Exception:
            print("  - hpa: (not found)")

    # Pods (via deployment selector)
    selector = (
        deploy.get("spec", {})
        .get("selector", {})
        .get("matchLabels", {})
        if isinstance(deploy, dict)
        else {}
    )
    if selector:
        sel_str = ",".join(f"{k}={v}" for k, v in selector.items())
        try:
            pods = _kubectl_json(["get", "pods", "-n", namespace, "-l", sel_str, "-o", "json"]).get(
                "items", []
            )
            ready = sum(1 for p in pods if _pod_is_ready(p))
            print(f"  - pods: total={len(pods)}  ready={ready}  selector={sel_str}")
        except Exception as exc:
            print(f"  - pods: ERROR: {exc}")

    # --- Prometheus snapshot ---
    prom_snapshot: Snapshot | None = None
    recomputed_action: str | None = None

    if args.no_prom:
        print("")
        print("prometheus_snapshot: (skipped via --no-prom)")
    else:
        print("")
        print("prometheus_snapshot:")
        local_port = _free_port()
        try:
            with _prometheus_port_forward(args.prom_namespace, args.prom_service, local_port) as prom:
                if not target:
                    raise RuntimeError("missing targetRef.name in OrchestratedRollout spec")

                target_pod_re = f"{target}-.*"

                # RPS
                rps_val, rps_src, _ = _query_first(
                    prom,
                    [
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[30s]))',
                            "inference_rps_30s",
                        ),
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[1m]))',
                            "inference_rps_1m",
                        ),
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}"}}[1m]))',
                            "all_rps_1m",
                        ),
                    ],
                )

                # RPS 5m for trend
                rps_5m, _, _ = _query_first(
                    prom,
                    [
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[5m]))',
                            "inference_rps_5m",
                        ),
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}"}}[5m]))',
                            "all_rps_5m",
                        ),
                    ],
                )

                # P95 latency (seconds → ms)
                p95_s, p95_src, _ = _query_first(
                    prom,
                    [
                        (
                            f'histogram_quantile(0.95, sum(rate(workload_request_duration_seconds_bucket'
                            f'{{kubernetes_namespace="{namespace}",kubernetes_pod_name=~"{target_pod_re}",'
                            f'endpoint="inference"}}[1m])) by (le))',
                            "p95_1m",
                        ),
                        (
                            f'histogram_quantile(0.95, sum(rate(workload_request_duration_seconds_bucket'
                            f'{{kubernetes_namespace="{namespace}",kubernetes_pod_name=~"{target_pod_re}"}}[1m]))'
                            f' by (le))',
                            "p95_all_1m",
                        ),
                    ],
                )
                p95_ms = p95_s * 1000.0

                # Error rate
                err_val, err_src, _ = _query_first(
                    prom,
                    [
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference",status=~"5.."}}[1m]))'
                            f' / sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",endpoint="inference"}}[1m]))',
                            "err_inference_1m",
                        ),
                        (
                            f'sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}",status=~"5.."}}[1m]))'
                            f' / sum(rate(workload_requests_total{{kubernetes_namespace="{namespace}",'  # noqa: E501
                            f'kubernetes_pod_name=~"{target_pod_re}"}}[1m]))',
                            "err_all_1m",
                        ),
                    ],
                )

                pending, _, _ = _query_first(prom, [('sum(kube_pod_status_phase{phase="Pending"})', "pending")])
                cpu, _, _ = _query_first(
                    prom,
                    [("avg(1 - rate(node_cpu_seconds_total{mode=\"idle\"}[2m]))", "cpu")],
                )
                mem, _, _ = _query_first(
                    prom,
                    [("avg(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))", "mem")],
                )

                # HPA desired/current (fallback to kubectl if no data)
                hpa_desired, _, _ = _query_first(
                    prom,
                    [
                        (
                            f'kube_horizontalpodautoscaler_status_desired_replicas'
                            f'{{namespace="{namespace}",horizontalpodautoscaler="{hpa_name}"}}',
                            "hpa_desired",
                        )
                    ],
                )
                hpa_current, _, _ = _query_first(
                    prom,
                    [
                        (
                            f'kube_horizontalpodautoscaler_status_current_replicas'
                            f'{{namespace="{namespace}",horizontalpodautoscaler="{hpa_name}"}}',
                            "hpa_current",
                        )
                    ],
                )

                # Trend → forecast (matches controller.snapshot)
                rps_trend = 0.0
                if rps_val > 0:
                    rps_trend = (rps_val - rps_5m) / max(rps_val, 1.0)

                if rps_trend >= 0.3:
                    stress_forecast = min(rps_trend, 1.0)
                elif rps_trend > 0.0:
                    stress_forecast = rps_trend
                else:
                    stress_forecast = 0.0

                prom_snapshot = Snapshot(
                    rps=float(rps_val),
                    p95_latency_ms=float(p95_ms),
                    error_rate=float(err_val) if math.isfinite(err_val) else 0.0,
                    pending_pods=int(pending),
                    node_cpu_util=float(cpu),
                    node_mem_util=float(mem),
                    hpa_desired_replicas=int(hpa_desired) if hpa_desired else 0,
                    hpa_current_replicas=int(hpa_current) if hpa_current else 0,
                    stress_forecast=float(stress_forecast),
                    degraded=False,
                )

                print(f"  - rps:           {prom_snapshot.rps:.2f}  (source={rps_src})")
                print(f"  - p95_latency:    {prom_snapshot.p95_latency_ms:.1f} ms  (source={p95_src})")
                print(f"  - error_rate:     {prom_snapshot.error_rate:.4f}  (source={err_src})")
                print(f"  - pending_pods:   {prom_snapshot.pending_pods}")
                print(f"  - node_cpu_util:  {prom_snapshot.node_cpu_util:.3f}")
                print(f"  - node_mem_util:  {prom_snapshot.node_mem_util:.3f}")
                print(
                    "  - hpa:            "
                    f"current={prom_snapshot.hpa_current_replicas}  desired={prom_snapshot.hpa_desired_replicas}"
                )
                print(f"  - rps_trend:      {rps_trend:.3f}")
                print(f"  - stress_forecast:{prom_snapshot.stress_forecast:.3f}")

        except Exception as exc:
            print(f"  ERROR: {exc}")

    # --- Recompute policy decision from current snapshot ---
    if prom_snapshot is not None:
        print("")
        print("recomputed_decision (from current snapshot):")

        # Stress
        stress_calc = Calculator()
        stress_score = stress_calc.compute(prom_snapshot)
        print(f"  - stress_score:   {stress_score:.3f}")

        # Policy
        policy_dir: Path = args.policy_dir
        if not (policy_dir / "policy_artifact.json").exists():
            print(f"  - policy: ERROR: policy_artifact.json not found under: {policy_dir}")
        else:
            engine = Engine(str(policy_dir))
            allowed_actions = action_set if isinstance(action_set, list) and action_set else None
            recomputed_action, policy_ver = engine.select_action(
                prom_snapshot, stress_score, allowed_actions=allowed_actions
            )

            # Guardrails (use CR guardrail config if present)
            guard = Guardrails()
            final_action, overridden, reason = guard.apply(
                recomputed_action, prom_snapshot, stress_score, config=guardrail_cfg
            )

            print(f"  - policy_version: {policy_ver}")
            print(f"  - policy_action:  {recomputed_action}")
            if overridden:
                print(f"  - guardrails:     OVERRIDDEN → {final_action}  ({reason})")
            else:
                print(f"  - guardrails:     ok → {final_action}")

            # Explain state key + Q-values (best-effort; internal API)
            try:
                state_key = engine._discretise(prom_snapshot, stress_score)
                feat_names = [f.name for f in engine._artifact.features]
                bins = state_key.split("_")
                print(
                    "  - state_key:     "
                    + state_key
                    + "  ("
                    + ", ".join(f"{n}={b}" for n, b in zip(feat_names, bins))
                    + ")"
                )

                q_values = engine._artifact.q_table.get(state_key, [])
                if q_values:
                    print("  - q_values:")
                    for a, q in zip(engine._artifact.actions, q_values):
                        mark = "<-- chosen" if a == recomputed_action else ""
                        print(f"      {a:9s}  Q={q:8.3f}  {mark}")
            except Exception:
                pass

    # Note about differences
    chosen = status.get("chosenStrategy")
    if prom_snapshot is not None and recomputed_action and chosen and chosen != recomputed_action:
        print("")
        print(
            "note: status.chosenStrategy was computed at deploy-time; recomputed_decision uses current metrics and may differ."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

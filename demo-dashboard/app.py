import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
except Exception:  # pragma: no cover - allows local static preview without k8s deps
    client = None
    config = None
    ApiException = Exception

APP_TITLE = "Adaptive Deployment Orchestration Demo"
CRD_GROUP = os.getenv("OROLL_GROUP", "rollout.orchestrated.io")
CRD_VERSION = os.getenv("OROLL_VERSION", "v1alpha1")
CRD_PLURAL = os.getenv("OROLL_PLURAL", "orchestratedrollouts")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")
ARGO_NAMESPACE = os.getenv("ARGO_NAMESPACE", "argocd")
DEFAULT_NAMESPACES = os.getenv(
    "DEMO_NAMESPACES",
    "workload-go,workload-cpu,workload-io,workload-mobilenet,workload-squeezenet,workload-tabular",
)

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

_k8s_loaded = False


def _load_k8s() -> bool:
    global _k8s_loaded
    if client is None or config is None:
        return False
    if _k8s_loaded:
        return True
    try:
        config.load_incluster_config()
    except Exception:
        try:
            config.load_kube_config()
        except Exception:
            return False
    _k8s_loaded = True
    return True


def _namespaces() -> List[str]:
    return [n.strip() for n in DEFAULT_NAMESPACES.split(",") if n.strip()]


def _safe_get(dct: Dict[str, Any], path: List[str], default: Any = None) -> Any:
    cur: Any = dct
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _latest_rollout() -> Dict[str, Any]:
    if not _load_k8s():
        return {"available": False, "message": "Kubernetes client is not configured"}
    api = client.CustomObjectsApi()
    items: List[Dict[str, Any]] = []
    for namespace in _namespaces():
        try:
            resp = api.list_namespaced_custom_object(CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL)
            for item in resp.get("items", []):
                item["_namespace"] = namespace
                items.append(item)
        except ApiException:
            continue
    if not items:
        return {"available": False, "message": "No OrchestratedRollout objects found in demo namespaces"}
    items.sort(
        key=lambda obj: (
            _parse_ts(_safe_get(obj, ["status", "decisionTimestamp"])),
            _parse_ts(_safe_get(obj, ["metadata", "creationTimestamp"])),
        ),
        reverse=True,
    )
    obj = items[0]
    spec = obj.get("spec", {}) or {}
    status = obj.get("status", {}) or {}
    hints = spec.get("rolloutHints", {}) or {}
    release = spec.get("release", {}) or {}
    target = spec.get("targetRef", {}) or {}
    strategy = status.get("chosenStrategy") or "pending"
    headroom = status.get("preScaleExtraReplicas")
    stress = status.get("stressScore")
    phase = status.get("phase") or "Pending"
    explanation = _explain_decision(strategy, headroom, hints, stress)
    return {
        "available": True,
        "namespace": obj.get("_namespace"),
        "name": _safe_get(obj, ["metadata", "name"], "unknown"),
        "created": _safe_get(obj, ["metadata", "creationTimestamp"]),
        "decisionTimestamp": status.get("decisionTimestamp"),
        "targetName": target.get("name", "unknown"),
        "releaseImage": release.get("image", "unknown"),
        "releaseTag": release.get("tag", "unknown"),
        "trafficProfile": hints.get("trafficProfile", "unknown"),
        "faultContext": hints.get("faultContext", "unknown"),
        "objective": hints.get("objective", "unknown"),
        "policyVariant": hints.get("policyVariant", status.get("policyVersion", "default")),
        "phase": phase,
        "strategy": strategy,
        "headroom": headroom,
        "stressScore": stress,
        "policyVersion": status.get("policyVersion", "unknown"),
        "message": status.get("message", ""),
        "explanation": explanation,
    }


def _explain_decision(strategy: str, headroom: Any, hints: Dict[str, Any], stress: Any) -> str:
    traffic = hints.get("trafficProfile", "unknown")
    fault = hints.get("faultContext", "unknown")
    objective = hints.get("objective", "unknown")
    if strategy == "rolling":
        return f"Low-risk {traffic}/{fault} rollout; the contextual policy avoided unnecessary headroom."
    if strategy == "pre-scale":
        extra = f" +{headroom}" if headroom is not None else ""
        return f"Reliability objective under {traffic}/{fault} context; pre-scale{extra} adds capacity before traffic shift."
    if strategy == "canary":
        return "The controller selected gradual traffic exposure to reduce release risk."
    if strategy == "delay":
        return "The controller deferred deployment because the observed state exceeded safety guardrails."
    if stress is not None:
        return f"Decision is pending; latest stress score is {stress}."
    return "Waiting for the controller to record its decision."


def _hpa_summary(namespace: Optional[str], target_name: Optional[str]) -> Dict[str, Any]:
    if not namespace or not target_name or not _load_k8s():
        return {"available": False}
    api = client.AutoscalingV2Api()
    # Helm charts in this repo use both `<workload>` and `<workload>-hpa` naming.
    for hpa_name in (target_name, f"{target_name}-hpa"):
        try:
            hpa = api.read_namespaced_horizontal_pod_autoscaler(hpa_name, namespace)
            return {
                "available": True,
                "name": hpa.metadata.name,
                "minReplicas": hpa.spec.min_replicas,
                "maxReplicas": hpa.spec.max_replicas,
                "currentReplicas": hpa.status.current_replicas,
                "desiredReplicas": hpa.status.desired_replicas,
            }
        except Exception:
            continue
    return {"available": False}


def _deployment_summary(namespace: Optional[str], target_name: Optional[str]) -> Dict[str, Any]:
    if not namespace or not target_name or not _load_k8s():
        return {"available": False}
    try:
        api = client.AppsV1Api()
        dep = api.read_namespaced_deployment(target_name, namespace)
        image = dep.spec.template.spec.containers[0].image if dep.spec.template.spec.containers else "unknown"
        return {
            "available": True,
            "image": image,
            "replicas": dep.status.replicas or 0,
            "readyReplicas": dep.status.ready_replicas or 0,
            "updatedReplicas": dep.status.updated_replicas or 0,
        }
    except Exception:
        return {"available": False}


def _argo_summary(target_name: Optional[str]) -> Dict[str, Any]:
    if not target_name or not _load_k8s():
        return {"available": False}
    app_name_map = {
        "go-service": "workload-go-service",
        "cpu-bound-fastapi": "workload-cpu-bound-fastapi",
        "io-latency-node": "workload-io-latency-node",
        "mobilenetv2-onnx": "workload-mobilenetv2-onnx",
        "squeezenet-onnx": "workload-squeezenet-onnx",
        "tabular-sklearn": "workload-tabular-sklearn",
    }
    app_name = app_name_map.get(target_name)
    if not app_name:
        return {"available": False}
    try:
        api = client.CustomObjectsApi()
        obj = api.get_namespaced_custom_object("argoproj.io", "v1alpha1", ARGO_NAMESPACE, "applications", app_name)
        return {
            "available": True,
            "name": app_name,
            "sync": _safe_get(obj, ["status", "sync", "status"], "unknown"),
            "health": _safe_get(obj, ["status", "health", "status"], "unknown"),
            "revision": _safe_get(obj, ["status", "sync", "revision"], "unknown"),
        }
    except Exception:
        return {"available": False}


def _prometheus_metrics(namespace: Optional[str], target_name: Optional[str]) -> Dict[str, Any]:
    if not namespace or not target_name:
        return {"available": False}
    queries = {
        "p95LatencySeconds": (
            f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
            f'{{kubernetes_namespace="{namespace}",kubernetes_pod_name=~"{target_name}.*"}}[2m])) by (le))'
        ),
        "throughputRps": (
            f'sum(rate(requests_total{{kubernetes_namespace="{namespace}",'
            f'kubernetes_pod_name=~"{target_name}.*"}}[2m]))'
        ),
        "failuresRps": (
            f'sum(rate(request_failures_total{{kubernetes_namespace="{namespace}",'
            f'kubernetes_pod_name=~"{target_name}.*"}}[2m]))'
        ),
    }
    output: Dict[str, Any] = {"available": True}
    for name, query in queries.items():
        try:
            resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=2)
            resp.raise_for_status()
            result = resp.json().get("data", {}).get("result", [])
            output[name] = float(result[0]["value"][1]) if result else None
        except Exception:
            output[name] = None
    if all(output.get(k) is None for k in queries):
        output["available"] = False
    return output


@app.get("/api/latest-rollout")
def api_latest_rollout() -> Dict[str, Any]:
    rollout = _latest_rollout()
    ns = rollout.get("namespace")
    target = rollout.get("targetName")
    return {
        "rollout": rollout,
        "deployment": _deployment_summary(ns, target),
        "hpa": _hpa_summary(ns, target),
        "metrics": _prometheus_metrics(ns, target),
        "argo": _argo_summary(target),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request, "title": APP_TITLE})

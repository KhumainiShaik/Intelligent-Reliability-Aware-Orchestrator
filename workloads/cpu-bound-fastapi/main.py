import math
import os
import time
from fastapi import FastAPI, Response
from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest
import uvicorn

PORT = int(os.getenv("PORT", "8080"))
WORKLOAD_NAME = os.getenv("WORKLOAD_NAME", "cpu-bound-fastapi")
WORKLOAD_CLASS = os.getenv("WORKLOAD_CLASS", "http")
WORKLOAD_KIND = os.getenv("WORKLOAD_KIND", "http")
VERSION = os.getenv("VERSION", "v1.0.0")
CPU_WORK_MS = int(os.getenv("CPU_WORK_MS", "25"))
WARMUP_SECONDS = float(os.getenv("WARMUP_SECONDS", "2"))
STARTED = time.monotonic()

requests_total = Counter("requests_total", "Total requests", ["endpoint", "status"])
failures_total = Counter("request_failures_total", "Failed requests", ["endpoint"])
http_duration = Histogram("http_request_duration_seconds", "HTTP request duration", ["endpoint"])
processing_duration = Histogram("request_processing_seconds", "Request processing duration", ["endpoint"])
ready_gauge = Gauge("workload_ready", "Workload readiness")
model_load_seconds = Gauge("model_load_seconds", "Warm-up/model-load duration")
model_load_seconds.set(WARMUP_SECONDS)

app = FastAPI(title=WORKLOAD_NAME, version=VERSION)

def ready() -> bool:
    is_ready = time.monotonic() - STARTED >= WARMUP_SECONDS
    ready_gauge.set(1 if is_ready else 0)
    return is_ready

def burn_cpu(ms: int) -> float:
    deadline = time.perf_counter() + ms / 1000.0
    value = 0.0
    i = 1
    while time.perf_counter() < deadline:
        value += math.sqrt(i % 1000) * math.sin(i)
        i += 1
    return value

@app.get("/healthz")
def healthz():
    requests_total.labels("healthz", "200").inc()
    return Response("ok\n", media_type="text/plain")

@app.get("/readyz")
def readyz():
    if ready():
        requests_total.labels("readyz", "200").inc()
        return Response("ready\n", media_type="text/plain")
    requests_total.labels("readyz", "503").inc()
    return Response("warming\n", status_code=503, media_type="text/plain")

@app.api_route("/infer", methods=["GET", "POST"])
def infer():
    start = time.monotonic()
    if not ready():
        failures_total.labels("infer").inc()
        requests_total.labels("infer", "503").inc()
        return {"status": "not_ready", "workload": WORKLOAD_NAME}
    value = burn_cpu(CPU_WORK_MS)
    elapsed = time.monotonic() - start
    http_duration.labels("infer").observe(elapsed)
    processing_duration.labels("infer").observe(elapsed)
    requests_total.labels("infer", "200").inc()
    return {"status": "ok", "workload": WORKLOAD_NAME, "version": VERSION, "cpu_work_ms": CPU_WORK_MS, "score": round(value, 4), "latency_ms": round(elapsed * 1000, 2)}

@app.get("/version")
def version():
    requests_total.labels("version", "200").inc()
    return {"workload": WORKLOAD_NAME, "class": WORKLOAD_CLASS, "kind": WORKLOAD_KIND, "version": VERSION}

@app.get("/metrics")
def metrics():
    ready()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

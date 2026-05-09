import os
import time
from fastapi import FastAPI, Response
from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest
import uvicorn

PORT = int(os.getenv("PORT", "8080"))
WORKLOAD_NAME = os.getenv("WORKLOAD_NAME", "tabular-sklearn")
WORKLOAD_CLASS = os.getenv("WORKLOAD_CLASS", "ml")
WORKLOAD_KIND = os.getenv("WORKLOAD_KIND", "ml")
VERSION = os.getenv("VERSION", "v1.0.0")
WARMUP_SECONDS = float(os.getenv("WARMUP_SECONDS", "1"))
STARTED = time.monotonic()

requests_total = Counter("requests_total", "Total requests", ["endpoint", "status"])
failures_total = Counter("request_failures_total", "Failed requests", ["endpoint"])
http_duration = Histogram("http_request_duration_seconds", "HTTP request duration", ["endpoint"])
inference_duration = Histogram("inference_duration_seconds", "Inference duration", ["endpoint"])
ready_gauge = Gauge("model_ready", "Model readiness")
model_load_seconds = Gauge("model_load_seconds", "Model load duration")
model_load_seconds.set(WARMUP_SECONDS)

app = FastAPI(title=WORKLOAD_NAME, version=VERSION)
WEIGHTS = [0.31, -0.12, 0.44, 0.08, -0.27, 0.19]
BIAS = -0.03

def ready() -> bool:
    is_ready = time.monotonic() - STARTED >= WARMUP_SECONDS
    ready_gauge.set(1 if is_ready else 0)
    return is_ready

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + pow(2.718281828, -x))

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
    return Response("loading\n", status_code=503, media_type="text/plain")

@app.api_route("/infer", methods=["GET", "POST"])
def infer():
    start = time.monotonic()
    if not ready():
        failures_total.labels("infer").inc()
        requests_total.labels("infer", "503").inc()
        return {"status": "not_ready", "workload": WORKLOAD_NAME}
    features = [0.2, 1.4, -0.7, 0.9, 0.1, -1.1]
    score = BIAS + sum(w * x for w, x in zip(WEIGHTS, features))
    probability = sigmoid(score)
    elapsed = time.monotonic() - start
    http_duration.labels("infer").observe(elapsed)
    inference_duration.labels("infer").observe(elapsed)
    requests_total.labels("infer", "200").inc()
    return {"status": "ok", "workload": WORKLOAD_NAME, "version": VERSION, "probability": round(probability, 6), "class": int(probability > 0.5), "latency_ms": round(elapsed * 1000, 3)}

@app.get("/version")
def version():
    requests_total.labels("version", "200").inc()
    return {"workload": WORKLOAD_NAME, "class": WORKLOAD_CLASS, "kind": WORKLOAD_KIND, "version": VERSION, "framework": "deterministic-tabular"}

@app.get("/metrics")
def metrics():
    ready()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

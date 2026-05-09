"""
ML Inference service for OrchestratedRollout experiments.

A real MobileNetV2 image-classification model served via FastAPI + ONNX Runtime.
Exposes Prometheus metrics compatible with the OrchestratedRollout controller
(same metric names as the Go workload so the controller works without changes).

Environment variables
---------------------
MODEL_PATH         Path to the ONNX model file       (default: /app/model/mobilenetv2.onnx)
PORT               HTTP listen port                   (default: 8080)
VERSION            Application version string         (default: v1.0.0)
WARMUP_DELAY_SECS  Minimum seconds before ready       (default: 8)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ml-workload")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_PATH = os.getenv("MODEL_PATH", "/app/model/mobilenetv2.onnx")
PORT = int(os.getenv("PORT", "8080"))
VERSION = os.getenv("VERSION", "v1.0.0")
WARMUP_DELAY_SECS = float(os.getenv("WARMUP_DELAY_SECS", "8"))

# ---------------------------------------------------------------------------
# Prometheus metrics — same names as the Go workload for controller compat
# ---------------------------------------------------------------------------
requests_total = Counter(
    "workload_requests_total",
    "Total number of requests by endpoint and status.",
    ["endpoint", "status"],
)
request_duration = Histogram(
    "workload_request_duration_seconds",
    "Request duration in seconds.",
    ["endpoint"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
in_flight_requests = Gauge(
    "workload_in_flight_requests",
    "Current number of in-flight requests.",
)
model_load_duration = Gauge(
    "workload_model_load_duration_seconds",
    "Time taken to load the ONNX model.",
)
app_info = Info("workload", "Application version info")

# ---------------------------------------------------------------------------
# Model state
# ---------------------------------------------------------------------------
_session: ort.InferenceSession | None = None
_input_name: str = ""
_ready = threading.Event()


def _load_model() -> None:
    """Load the ONNX model and run warm-up inferences."""
    global _session, _input_name

    logger.info("Loading ONNX model from %s …", MODEL_PATH)
    t0 = time.monotonic()

    _session = ort.InferenceSession(
        MODEL_PATH,
        providers=["CPUExecutionProvider"],
    )
    _input_name = _session.get_inputs()[0].name

    # Warm-up: a few dummy forward passes to initialise the runtime
    dummy = np.random.randn(1, 3, 224, 224).astype(np.float32)
    for _ in range(3):
        _session.run(None, {_input_name: dummy})

    load_secs = time.monotonic() - t0
    model_load_duration.set(load_secs)
    logger.info("Model loaded and warmed up in %.2f s", load_secs)

    # Honour a minimum warm-up delay (realistic for large models)
    remaining = max(0.0, WARMUP_DELAY_SECS - load_secs)
    if remaining > 0:
        logger.info("Extra warm-up delay: %.1f s", remaining)
        time.sleep(remaining)

    _ready.set()
    logger.info("Service READY for inference requests")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    app_info.info({"version": VERSION})
    thread = threading.Thread(target=_load_model, daemon=True)
    thread.start()
    yield


app = FastAPI(
    title="ML Inference Workload",
    version=VERSION,
    lifespan=lifespan,
)


# ---- health / readiness ---------------------------------------------------
@app.get("/healthz")
async def healthz():
    requests_total.labels(endpoint="healthz", status="200").inc()
    return PlainTextResponse("ok\n")


@app.get("/readyz")
async def readyz():
    if _ready.is_set():
        requests_total.labels(endpoint="readyz", status="200").inc()
        return PlainTextResponse("ready\n")
    requests_total.labels(endpoint="readyz", status="503").inc()
    return PlainTextResponse("not ready (loading model)\n", status_code=503)


# ---- inference -------------------------------------------------------------
@app.api_route("/inference", methods=["GET", "POST"])
async def inference(request: Request):
    """
    Run MobileNetV2 inference on a random 224×224 RGB tensor.

    In a production setting you would decode an uploaded image here;
    the random tensor is sufficient for rollout experiments because
    the identical model-loading, memory, and compute characteristics
    are preserved.
    """
    in_flight_requests.inc()
    start = time.monotonic()

    try:
        if not _ready.is_set():
            dur = time.monotonic() - start
            request_duration.labels(endpoint="inference").observe(dur)
            requests_total.labels(endpoint="inference", status="503").inc()
            return JSONResponse(
                {"status": "not_ready", "version": VERSION},
                status_code=503,
            )

        # Real ONNX Runtime inference (CPU-bound, realistic latency)
        input_data = np.random.randn(1, 3, 224, 224).astype(np.float32)
        outputs = _session.run(None, {_input_name: input_data})
        logits = outputs[0][0]

        # Top-5 softmax predictions
        exp_logits = np.exp(logits - logits.max())  # numerically stable
        probs = exp_logits / exp_logits.sum()
        top5 = np.argsort(probs)[-5:][::-1]

        predictions = [
            {"class_id": int(c), "probability": round(float(probs[c]), 6)}
            for c in top5
        ]

        dur = time.monotonic() - start
        request_duration.labels(endpoint="inference").observe(dur)
        requests_total.labels(endpoint="inference", status="200").inc()

        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "model": "mobilenetv2",
            "latency_ms": round(dur * 1000, 2),
            "predictions": predictions,
        })

    finally:
        in_flight_requests.dec()


# ---- Prometheus metrics ----------------------------------------------------
@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---- root info -------------------------------------------------------------
@app.get("/")
async def root():
    requests_total.labels(endpoint="root", status="200").inc()
    return JSONResponse({
        "service": "orchestrated-rollout-ml-workload",
        "model": "mobilenetv2",
        "version": VERSION,
        "ready": _ready.is_set(),
    })

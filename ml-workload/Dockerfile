# ML Inference Workload — OrchestratedRollout
#
# Real MobileNetV2 (ONNX Runtime) served via FastAPI with Prometheus metrics
# compatible with the OrchestratedRollout controller.
#
# Pre-requisite:
#   python ml-workload/export_model.py      # generates ml-workload/model/mobilenetv2.onnx
#
# Build (from repo root):
#   docker build -f ml-workload/Dockerfile -t orchestrated-rollout-ml-workload:v1.0.0 .

FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime dependencies
COPY ml-workload/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Copy pre-exported ONNX model
COPY ml-workload/model/ /app/model/

# Copy application code
COPY ml-workload/app/ /app/

# Non-root user (same UID as the Go workload)
USER 65534

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

ENTRYPOINT ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]

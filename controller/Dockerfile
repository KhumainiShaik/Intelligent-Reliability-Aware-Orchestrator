# OrchestratedRollout Controller (Python / kopf)
# Stage 1: install dependencies into a virtualenv
FROM python:3.11-slim AS builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY controller/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Stage 2: slim runtime image
FROM python:3.11-slim AS runtime

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy controller source only
COPY controller/ controller/

# Non-root user (nobody already exists with UID 65534 on Debian)
USER 65534

# Kopf liveness probe port
EXPOSE 8081

# Default env vars (overridden in k8s deployment)
ENV PROMETHEUS_URL="http://prometheus.monitoring.svc.cluster.local:9090" \
    POLICY_PATH="/policy/artifact" \
    EPISODE_LOG_PATH="/episodes" \
    LOG_LEVEL="INFO" \
    LOG_FORMAT="json"

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8081/healthz')" || exit 1

ENTRYPOINT ["python", "-m", "controller.main"]

# Build stage
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux \
    go build -ldflags="-s -w" -o /workload ./workload/

# Runtime stage
FROM alpine:3.19
RUN apk --no-cache add ca-certificates
# Use existing nobody user (UID 65534) for non-root
COPY --from=builder /workload /usr/local/bin/workload

USER 65534
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/healthz || exit 1

ENTRYPOINT ["workload"]

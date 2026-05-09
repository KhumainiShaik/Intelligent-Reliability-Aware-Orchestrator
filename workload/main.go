// Package main implements a configurable Go workload service for rollout experiments.
// It exposes /healthz, /readyz, /metrics, and /inference endpoints with tunable
// warm-up delay, CPU work per request, and memory pressure.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// --- Configuration flags ---
var (
	port           = flag.Int("port", 8080, "HTTP listen port")
	warmupDelay    = flag.Duration("warmup-delay", 5*time.Second, "Simulated warm-up delay before readiness")
	cpuWorkMs      = flag.Int("cpu-work-ms", 10, "Simulated CPU work per request in milliseconds")
	memPressureMB  = flag.Int("mem-pressure-mb", 0, "Allocate N MB at startup to simulate memory pressure")
	version        = flag.String("version", "v1.0.0", "Application version (returned in responses)")
	bgGradient     = flag.String("bg-gradient", "#667eea, #764ba2", "CSS gradient colours for the HTML page")
)

// --- Prometheus metrics ---
var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "workload_requests_total",
		Help: "Total number of requests by endpoint and status.",
	}, []string{"endpoint", "status"})

	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "workload_request_duration_seconds",
		Help:    "Request duration in seconds.",
		Buckets: []float64{0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0},
	}, []string{"endpoint"})

	inFlightRequests = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "workload_in_flight_requests",
		Help: "Current number of in-flight requests.",
	})

	appInfo = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "workload_info",
		Help: "Application version info.",
	}, []string{"version"})
)

// ready is set to 1 after warm-up completes.
var ready int32

func main() {
	flag.Parse()

	log.Printf("workload starting: version=%s port=%d warmup=%s cpuWork=%dms memPressure=%dMB",
		*version, *port, *warmupDelay, *cpuWorkMs, *memPressureMB)

	appInfo.WithLabelValues(*version).Set(1)

	// Simulate memory pressure
	if *memPressureMB > 0 {
		_ = make([]byte, *memPressureMB*1024*1024)
		log.Printf("allocated %d MB for memory pressure simulation", *memPressureMB)
	}

	// Warm-up delay (readiness probe will fail until this completes)
	go func() {
		log.Printf("warm-up: waiting %s before becoming ready", *warmupDelay)
		time.Sleep(*warmupDelay)
		atomic.StoreInt32(&ready, 1)
		log.Printf("warm-up complete: service is ready")
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handleHealthz)
	mux.HandleFunc("/readyz", handleReadyz)
	mux.HandleFunc("/inference", handleInference)
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/", handleRoot)

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", *port),
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Graceful shutdown
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("listening on :%d", *port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down gracefully...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("shutdown error: %v", err)
	}
	log.Println("shutdown complete")
}

// handleHealthz always returns 200 (liveness).
func handleHealthz(w http.ResponseWriter, r *http.Request) {
	requestsTotal.WithLabelValues("healthz", "200").Inc()
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

// handleReadyz returns 200 only after warm-up completes (readiness).
func handleReadyz(w http.ResponseWriter, r *http.Request) {
	if atomic.LoadInt32(&ready) == 1 {
		requestsTotal.WithLabelValues("readyz", "200").Inc()
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ready")
	} else {
		requestsTotal.WithLabelValues("readyz", "503").Inc()
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintln(w, "not ready (warming up)")
	}
}

// handleInference simulates a compute-heavy inference request.
func handleInference(w http.ResponseWriter, r *http.Request) {
	inFlightRequests.Inc()
	defer inFlightRequests.Dec()

	start := time.Now()

	// Simulate CPU work
	simulateCPUWork(time.Duration(*cpuWorkMs) * time.Millisecond)

	duration := time.Since(start)
	requestDuration.WithLabelValues("inference").Observe(duration.Seconds())

	if atomic.LoadInt32(&ready) == 0 {
		requestsTotal.WithLabelValues("inference", "503").Inc()
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintf(w, `{"status":"not_ready","version":"%s","latency_ms":%.2f}`+"\n",
			*version, float64(duration.Microseconds())/1000.0)
		return
	}

	requestsTotal.WithLabelValues("inference", "200").Inc()
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"status":"ok","version":"%s","latency_ms":%.2f,"prediction":[0.42,0.58]}`+"\n",
		*version, float64(duration.Microseconds())/1000.0)
}

// handleRoot returns an HTML page showing the current version.
func handleRoot(w http.ResponseWriter, r *http.Request) {
	requestsTotal.WithLabelValues("root", "200").Inc()

	// If the client wants JSON, return JSON.
	accept := r.Header.Get("Accept")
	if accept == "application/json" {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"service":"orchestrated-rollout-workload","version":"%s","ready":%v}`+"\n",
			*version, atomic.LoadInt32(&ready) == 1)
		return
	}

	// Otherwise, return an HTML page with a visual version banner.
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, rootHTML, *version, *bgGradient, *version, *version)
}

const rootHTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Orchestrated Rollout Workload — %s</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh;
      background: linear-gradient(135deg, %s);
      color: #fff;
    }
    .card {
      background: rgba(255,255,255,0.15);
      backdrop-filter: blur(10px);
      border-radius: 20px; padding: 3rem 4rem;
      text-align: center; max-width: 500px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.2);
    }
    h1 { font-size: 3rem; margin-bottom: 0.5rem; }
    .version-badge {
      display: inline-block; font-size: 1.5rem; font-weight: bold;
      padding: 0.4rem 1.5rem; border-radius: 999px;
      background: rgba(255,255,255,0.25); margin: 1rem 0;
    }
    p { opacity: 0.9; line-height: 1.6; }
    .meta { margin-top: 1.5rem; font-size: 0.85rem; opacity: 0.7; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Workload</h1>
    <div class="version-badge">%s</div>
    <p>Orchestrated Rollout — RL-based Kubernetes deployment strategy selector</p>
    <div class="meta">Serving from pod · Version %s</div>
  </div>
</body>
</html>`

// simulateCPUWork burns CPU for approximately the given duration using math operations.
func simulateCPUWork(d time.Duration) {
	if d <= 0 {
		return
	}
	deadline := time.Now().Add(d)
	x := 1.0
	for time.Now().Before(deadline) {
		for i := 0; i < 1000; i++ {
			x = math.Sin(x)*math.Cos(x) + math.Sqrt(math.Abs(x)+1)
		}
	}
	_ = x
}

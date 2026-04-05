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

// handleRoot returns basic info.
func handleRoot(w http.ResponseWriter, r *http.Request) {
	requestsTotal.WithLabelValues("root", "200").Inc()
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"service":"orchestrated-rollout-workload","version":"%s","ready":%v}`+"\n",
		*version, atomic.LoadInt32(&ready) == 1)
}

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

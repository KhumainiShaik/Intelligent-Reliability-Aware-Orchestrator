/**
 * k6 Shared Configuration for Orchestrated Rollout experiments.
 * 
 * Provides common settings, thresholds, and helper functions
 * used across all load test scenarios.
 */

import exec from 'k6/execution';

// Workload endpoint (via NGINX Ingress)
export const BASE_URL = __ENV.TARGET_URL || 'http://workload.local';
export const INFERENCE_ENDPOINT = `${BASE_URL}/inference`;
export const HEALTH_ENDPOINT = `${BASE_URL}/healthz`;

// Default SLO thresholds
export const SLO_P95_LATENCY_MS = parseInt(__ENV.SLO_P95_MS || '100');
export const SLO_ERROR_RATE = parseFloat(__ENV.SLO_ERROR_RATE || '0.01');

// Common thresholds configured against cleanly isolated metrics strictly outside warmup
export const THRESHOLDS = {
    'inference_latency': [
        `p(95)<${SLO_P95_LATENCY_MS}`,    // p95 latency SLO
        `p(99)<${SLO_P95_LATENCY_MS * 2}`, // p99 at 2x SLO
    ],
    'error_rate': [`rate<${SLO_ERROR_RATE}`],
    'http_reqs': ['rate>0'],
};

// Common tags for Prometheus integration
export function getRequestParams(scenario) {
    const params = {
        tags: {
            scenario: scenario,
            experiment: __ENV.EXPERIMENT_ID || 'default',
        },
        timeout: '10s',
    };
    // When routing through NGINX Ingress, add Host header
    if (__ENV.HOST_HEADER) {
        params.headers = { Host: __ENV.HOST_HEADER };
    }
    return params;
}

// Helper to determine if we are in warmup stage
export function parseDuration(durStr) {
    if (!durStr) return 0;
    const match = durStr.match(/^(\d+)(s|m|ms)$/);
    if (!match) return 0;
    const val = parseInt(match[1], 10);
    const unit = match[2];
    if (unit === 's') return val * 1000;
    if (unit === 'm') return val * 60000;
    return val;
}

export function isWarmup() {
    const warmupDurStr = __ENV.WARMUP_DURATION || '30s';
    const warmupMs = parseDuration(warmupDurStr);
    return (Date.now() - exec.scenario.startTime) < warmupMs;
}

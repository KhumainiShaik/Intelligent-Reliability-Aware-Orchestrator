/**
 * Alibaba Cluster-Trace Pressure Scenario
 *
 * Models realistic cluster resource contention derived from the Alibaba
 * Cluster Trace v2018 (≈4,000 machines, 8 days of mixed online + batch
 * workloads).  The trace reveals periodic pressure waves driven by
 * batch job submissions and co-located online service load.
 *
 * This scenario simulates a deployment occurring during a rising
 * cluster pressure wave — the kind of condition where rollout strategy
 * selection matters most:
 *   1. Moderate steady load (services running normally)
 *   2. Gradual pressure build (batch jobs consuming headroom)
 *   3. Peak contention window (deployment occurs here)
 *   4. Extended high-pressure plateau (batch + online overlap)
 *   5. Gradual pressure relief (batch completes)
 *   6. Return to moderate baseline
 *
 * Reference:
 *   Lu, C. et al. (2017). "Imbalance in the Cloud: An Analysis on
 *   Alibaba Cluster Trace." IEEE BigData 2017.
 *   Dataset: https://github.com/alibaba/clusterdata
 *
 * Usage:
 *   TARGET_URL=http://localhost:8080 k6 run k6/scenarios/alibaba_pressure.js
 */

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';
import { INFERENCE_ENDPOINT, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);
const sloViolations = new Counter('slo_violations');

// Parameters calibrated from Alibaba trace utilisation distributions:
// p50 CPU ~35%, p75 ~55%, p90 ~72%, p99 ~88% across machines
const MODERATE_RPS = parseInt(__ENV.MODERATE_RPS || '100');
const PRESSURE_RPS = parseInt(__ENV.PRESSURE_RPS || '250');   // ~2.5× (p75→p90 transition)
const PEAK_RPS = parseInt(__ENV.PEAK_RPS || '350');           // ~3.5× (p90+ contention)
const RELIEF_RPS = parseInt(__ENV.RELIEF_RPS || '150');       // batch draining

export const options = {
    scenarios: {
        alibaba_pressure: {
            executor: 'ramping-arrival-rate',
            startRate: MODERATE_RPS,
            timeUnit: '1s',
            preAllocatedVUs: Math.ceil(PEAK_RPS * 0.6),
            maxVUs: PEAK_RPS * 3,
            stages: [
                // Phase 1: Moderate steady-state (online services, low batch)
                { duration: '2m',  target: MODERATE_RPS },

                // Phase 2: Gradual pressure build (batch submission wave)
                { duration: '1m',  target: PRESSURE_RPS },

                // Phase 3: Peak contention — this is where deployment happens
                { duration: '30s', target: PEAK_RPS },
                { duration: '2m',  target: PEAK_RPS },

                // Phase 4: Extended high-pressure plateau (batch + online overlap)
                { duration: '2m',  target: Math.ceil(PEAK_RPS * 0.85) },

                // Phase 5: Gradual pressure relief (batch completing)
                { duration: '1m',  target: RELIEF_RPS },

                // Phase 6: Return to moderate baseline
                { duration: '30s', target: MODERATE_RPS },
                { duration: '1m',  target: MODERATE_RPS },
            ],
        },
    },
    thresholds: THRESHOLDS,
};

export default function () {
    const params = getRequestParams('alibaba_pressure');
    const res = http.get(INFERENCE_ENDPOINT, params);

    const isOk = check(res, {
        'status is 200': (r) => r.status === 200,
        'latency under SLO': (r) => r.timings.duration < 100,
    });

    if (!isWarmup()) {
        errorRate.add(res.status !== 200);
        latencyTrend.add(res.timings.duration);
    }

    if (!isOk) {
        sloViolations.add(1);
    }
}

export function handleSummary(data) {
    return {
        'stdout': JSON.stringify(data, null, 2),
        'results/alibaba_pressure_summary.json': JSON.stringify(data),
    };
}

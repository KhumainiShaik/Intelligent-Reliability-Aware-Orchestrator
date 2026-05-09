/**
 * Azure Functions Multi-Tenant Burst Scenario
 *
 * Models the bursty, multi-tenant invocation patterns observed in the
 * Azure Functions public trace dataset.  Serverless workloads exhibit
 * highly irregular request-rate envelopes: long idle/low periods
 * punctuated by synchronised tenant bursts (e.g., cron-triggered
 * functions firing at the same minute boundary).
 *
 * The shape implemented here is:
 *   1. Low baseline (idle tenants)
 *   2. First co-firing burst (3× within 15 seconds)
 *   3. Partial drain back to 1.5× baseline
 *   4. Second, larger burst (5× — represents batch + cron overlap)
 *   5. Extended elevated plateau (batch processing tail)
 *   6. Cooldown to baseline
 *
 * Reference:
 *   Shahrad, M. et al. (2020). "Serverless in the Wild: Characterizing
 *   and Optimizing the Serverless Workload at a Large Cloud Provider."
 *   USENIX ATC '20.
 *   Dataset: https://github.com/Azure/AzurePublicDataset
 *
 * Usage:
 *   TARGET_URL=http://localhost:8080 k6 run k6/scenarios/azure_functions_burst.js
 */

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';
import { INFERENCE_ENDPOINT, SUMMARY_TREND_STATS, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);
const sloViolations = new Counter('slo_violations');

// Parameters derived from Azure Functions trace analysis
// Idle periods ~20 RPS; co-firing bursts reach 3-5× within seconds
const IDLE_RPS = parseInt(__ENV.IDLE_RPS || '20');
const MINOR_BURST_RPS = parseInt(__ENV.MINOR_BURST_RPS || '60');   // 3×
const MAJOR_BURST_RPS = parseInt(__ENV.MAJOR_BURST_RPS || '120');  // 6×
const PLATEAU_RPS = parseInt(__ENV.PLATEAU_RPS || '80');           // batch tail

export const options = {
    scenarios: {
        azure_functions: {
            executor: 'ramping-arrival-rate',
            startRate: IDLE_RPS,
            timeUnit: '1s',
            preAllocatedVUs: Math.ceil(MAJOR_BURST_RPS * 0.6),
            maxVUs: MAJOR_BURST_RPS * 3,
            stages: [
                // Phase 1: Idle baseline (1 min)
                { duration: '1m',  target: IDLE_RPS },

                // Phase 2: First co-firing burst — 3× in 15s
                { duration: '15s', target: MINOR_BURST_RPS },
                { duration: '45s', target: MINOR_BURST_RPS },

                // Phase 3: Partial drain (30s)
                { duration: '30s', target: Math.ceil(IDLE_RPS * 1.5) },

                // Phase 4: Second major burst — 6× (cron + batch overlap)
                { duration: '10s', target: MAJOR_BURST_RPS },
                { duration: '1m',  target: MAJOR_BURST_RPS },

                // Phase 5: Extended elevated plateau — batch processing tail
                { duration: '30s', target: PLATEAU_RPS },
                { duration: '2m',  target: PLATEAU_RPS },

                // Phase 6: Cooldown to baseline
                { duration: '1m',  target: IDLE_RPS },
                { duration: '1m',  target: IDLE_RPS },
            ],
        },
    },
    thresholds: THRESHOLDS,
    summaryTrendStats: SUMMARY_TREND_STATS,
};

export default function () {
    const params = getRequestParams('azure_functions');
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
        'results/azure_functions_burst_summary.json': JSON.stringify(data),
    };
}

/**
 * WorldCup98-Inspired Burst Scenario
 *
 * Replays a request-rate shape derived from the 1998 FIFA World Cup
 * web access logs — one of the most cited bursty traffic datasets in
 * systems research (1.35 billion requests over 92 days).
 *
 * The original trace shows flash-crowd effects during popular matches:
 * a 4-6× traffic surge arriving in under 60 seconds, sustained for
 * 10-30 minutes, then a gradual decay.  This scenario models that
 * shape using k6's ramping-arrival-rate executor.
 *
 * Reference:
 *   Arlitt, M. & Jin, T. (2000). A Workload Characterization Study
 *   of the 1998 World Cup Web Site. IEEE Network, 14(3), 30-37.
 *   Dataset: https://ita.ee.lbl.gov/html/contrib/WorldCup.html
 *
 * Usage:
 *   TARGET_URL=http://localhost:8080 k6 run k6/scenarios/worldcup98_burst.js
 *   # Override parameters:
 *   BASE_RPS=50 BURST_RPS=400 k6 run k6/scenarios/worldcup98_burst.js
 */

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';
import { INFERENCE_ENDPOINT, SUMMARY_TREND_STATS, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);
const sloViolations = new Counter('slo_violations');

// WorldCup98-derived parameters
// Match days showed ~4-6× baseline surge in under 60s
const BASE_RPS = parseInt(__ENV.BASE_RPS || '80');
const BURST_RPS = parseInt(__ENV.BURST_RPS || '400');   // ~5× surge
const SECOND_BURST_RPS = parseInt(__ENV.SECOND_BURST_RPS || '300'); // 2nd smaller burst

// Timing derived from trace analysis:
// - Pre-match warm-up: gradual 1.5× rise over 2 minutes
// - Match start flash crowd: surge to 5× in ~30 seconds
// - Sustained peak: 8-15 minutes at peak
// - Half-time dip: drops to 2× for 2 minutes
// - Second-half surge: rises again to 3.5×
// - Post-match decay: exponential-like drop over 5 minutes
export const options = {
    scenarios: {
        worldcup98_burst: {
            executor: 'ramping-arrival-rate',
            startRate: BASE_RPS,
            timeUnit: '1s',
            preAllocatedVUs: Math.ceil(BURST_RPS * 0.6),
            maxVUs: BURST_RPS * 3,
            stages: [
                // Phase 1: Pre-match baseline (2 min warm-up)
                { duration: '2m',  target: Math.ceil(BASE_RPS * 1.5) },

                // Phase 2: Flash crowd — surge to 5× in 30s
                { duration: '30s', target: BURST_RPS },

                // Phase 3: Sustained peak (3 min at full burst)
                { duration: '3m',  target: BURST_RPS },

                // Phase 4: Half-time dip — drop to 2× (1 min)
                { duration: '30s', target: Math.ceil(BASE_RPS * 2) },
                { duration: '1m',  target: Math.ceil(BASE_RPS * 2) },

                // Phase 5: Second-half surge — 3.5× (2 min)
                { duration: '20s', target: SECOND_BURST_RPS },
                { duration: '2m',  target: SECOND_BURST_RPS },

                // Phase 6: Post-match decay — exponential-like drop (2 min)
                { duration: '30s', target: Math.ceil(BASE_RPS * 1.8) },
                { duration: '30s', target: Math.ceil(BASE_RPS * 1.2) },
                { duration: '1m',  target: BASE_RPS },
            ],
        },
    },
    thresholds: THRESHOLDS,
    summaryTrendStats: SUMMARY_TREND_STATS,
};

export default function () {
    const params = getRequestParams('worldcup98_burst');
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
        'results/worldcup98_burst_summary.json': JSON.stringify(data),
    };
}

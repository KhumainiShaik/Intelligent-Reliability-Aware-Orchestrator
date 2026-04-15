/**
 * Spike Load Scenario
 * 
 * Generates a sudden traffic burst to test system behaviour
 * under flash traffic spikes during rollouts.
 */

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { INFERENCE_ENDPOINT, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);

const BASE_RPS = parseInt(__ENV.BASE_RPS || '100');
const SPIKE_RPS = parseInt(__ENV.SPIKE_RPS || '500');
const WARMUP_DURATION = __ENV.WARMUP_DURATION || '1m';
const SPIKE_RAMP = __ENV.SPIKE_RAMP || '10s';
const SPIKE_DURATION = __ENV.SPIKE_DURATION || '1m';
const RECOVERY_RAMP = __ENV.RECOVERY_RAMP || '10s';
const RECOVERY_DURATION = __ENV.RECOVERY_DURATION || '2m';

export const options = {
    scenarios: {
        spike: {
            executor: 'ramping-arrival-rate',
            startRate: BASE_RPS,
            timeUnit: '1s',
            preAllocatedVUs: Math.ceil(SPIKE_RPS * 0.5),
            maxVUs: SPIKE_RPS * 3,
            stages: [
                { duration: WARMUP_DURATION, target: BASE_RPS },     // Warm up at base
                { duration: SPIKE_RAMP, target: SPIKE_RPS },         // Spike up (fast)
                { duration: SPIKE_DURATION, target: SPIKE_RPS },     // Hold spike
                { duration: RECOVERY_RAMP, target: BASE_RPS },       // Drop back
                { duration: RECOVERY_DURATION, target: BASE_RPS },   // Recovery period
            ],
        },
    },
    thresholds: THRESHOLDS,
};

export default function () {
    const params = getRequestParams('spike');
    const res = http.get(INFERENCE_ENDPOINT, params);

    check(res, {
        'status is 200': (r) => r.status === 200,
    });

    if (!isWarmup()) {
        errorRate.add(res.status !== 200);
        latencyTrend.add(res.timings.duration);
    }
}

export function handleSummary(data) {
    return {
        'stdout': JSON.stringify(data, null, 2),
        'results/spike_summary.json': JSON.stringify(data),
    };
}

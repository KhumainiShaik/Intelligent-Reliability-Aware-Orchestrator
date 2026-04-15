/**
 * Ramp Load Scenario
 * 
 * Gradually increases RPS to simulate organic traffic growth
 * and trigger HPA autoscaling during rollouts.
 */

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { INFERENCE_ENDPOINT, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);

const BASE_RPS = parseInt(__ENV.BASE_RPS || '50');
const PEAK_RPS = parseInt(__ENV.PEAK_RPS || '300');
const RAMP_DURATION = __ENV.RAMP_DURATION || '3m';
const HOLD_DURATION = __ENV.HOLD_DURATION || '2m';
const COOLDOWN_DURATION = __ENV.COOLDOWN_DURATION || '1m';

export const options = {
    scenarios: {
        ramp: {
            executor: 'ramping-arrival-rate',
            startRate: BASE_RPS,
            timeUnit: '1s',
            preAllocatedVUs: Math.ceil(PEAK_RPS * 0.5),
            maxVUs: PEAK_RPS * 2,
            stages: [
                { duration: RAMP_DURATION, target: PEAK_RPS },       // Ramp up
                { duration: HOLD_DURATION, target: PEAK_RPS },       // Hold at peak
                { duration: COOLDOWN_DURATION, target: BASE_RPS },   // Cool down
            ],
        },
    },
    thresholds: THRESHOLDS,
};

export default function () {
    const params = getRequestParams('ramp');
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
        'results/ramp_summary.json': JSON.stringify(data),
    };
}

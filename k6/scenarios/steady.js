/**
 * Steady Load Scenario
 * 
 * Generates constant RPS traffic to establish baseline behaviour.
 * Used to test rollouts under normal, stable load conditions.
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { INFERENCE_ENDPOINT, THRESHOLDS, getRequestParams, isWarmup } from '../config.js';

const errorRate = new Rate('error_rate');
const latencyTrend = new Trend('inference_latency', true);

const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '100');
const DURATION = __ENV.DURATION || '5m';

export const options = {
    scenarios: {
        steady: {
            executor: 'constant-arrival-rate',
            rate: TARGET_RPS,
            timeUnit: '1s',
            duration: DURATION,
            preAllocatedVUs: Math.ceil(TARGET_RPS * 0.5),
            maxVUs: TARGET_RPS * 2,
        },
    },
    thresholds: THRESHOLDS,
};

export default function () {
    const params = getRequestParams('steady');
    const res = http.get(INFERENCE_ENDPOINT, params);

    check(res, {
        'status is 200': (r) => r.status === 200,
        'response has prediction': (r) => {
            try {
                const body = JSON.parse(r.body);
                return body.prediction !== undefined;
            } catch {
                return false;
            }
        },
    });

    if (!isWarmup()) {
        errorRate.add(res.status !== 200);
        latencyTrend.add(res.timings.duration);
    }
}

export function handleSummary(data) {
    return {
        'stdout': JSON.stringify(data, null, 2),
        'results/steady_summary.json': JSON.stringify(data),
    };
}

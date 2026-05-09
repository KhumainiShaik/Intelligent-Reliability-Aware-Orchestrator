import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: __ENV.RAMP_UP || '20s', target: Number(__ENV.VUS || 8) },
    { duration: __ENV.HOLD || '40s', target: Number(__ENV.VUS || 8) },
    { duration: __ENV.RAMP_DOWN || '10s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<2000'],
  },
};

const baseUrl = __ENV.BASE_URL || 'http://localhost:8080';
const inferPath = __ENV.INFER_PATH || '/infer';
const workload = __ENV.WORKLOAD || 'portable-workload';

export default function () {
  const res = http.get(`${baseUrl}${inferPath}`, { tags: { workload } });
  check(res, {
    'infer status is 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  sleep(Number(__ENV.SLEEP_SECONDS || 0.2));
}

const express = require('express');
const client = require('prom-client');

const app = express();
const port = Number(process.env.PORT || 8080);
const workloadName = process.env.WORKLOAD_NAME || 'io-latency-node';
const workloadClass = process.env.WORKLOAD_CLASS || 'http';
const workloadKind = process.env.WORKLOAD_KIND || 'http';
const version = process.env.VERSION || 'v1.0.0';
const sleepMs = Number(process.env.SLEEP_MS || 80);
const warmupSeconds = Number(process.env.WARMUP_SECONDS || 2);
const started = Date.now();

const register = new client.Registry();
client.collectDefaultMetrics({ register });
const requestsTotal = new client.Counter({ name: 'requests_total', help: 'Total requests', labelNames: ['endpoint', 'status'], registers: [register] });
const failuresTotal = new client.Counter({ name: 'request_failures_total', help: 'Failed requests', labelNames: ['endpoint'], registers: [register] });
const httpDuration = new client.Histogram({ name: 'http_request_duration_seconds', help: 'HTTP request duration', labelNames: ['endpoint'], buckets: [0.005,0.01,0.025,0.05,0.1,0.25,0.5,1,2.5,5], registers: [register] });
const processingDuration = new client.Histogram({ name: 'request_processing_seconds', help: 'Request processing duration', labelNames: ['endpoint'], buckets: [0.005,0.01,0.025,0.05,0.1,0.25,0.5,1,2.5,5], registers: [register] });
const readyGauge = new client.Gauge({ name: 'workload_ready', help: 'Workload readiness', registers: [register] });
const modelLoadSeconds = new client.Gauge({ name: 'model_load_seconds', help: 'Warm-up/model-load duration', registers: [register] });
modelLoadSeconds.set(warmupSeconds);

function ready() {
  const isReady = (Date.now() - started) / 1000 >= warmupSeconds;
  readyGauge.set(isReady ? 1 : 0);
  return isReady;
}
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

app.get('/healthz', (_req, res) => { requestsTotal.inc({ endpoint: 'healthz', status: '200' }); res.type('text/plain').send('ok\n'); });
app.get('/readyz', (_req, res) => {
  if (ready()) { requestsTotal.inc({ endpoint: 'readyz', status: '200' }); res.type('text/plain').send('ready\n'); return; }
  requestsTotal.inc({ endpoint: 'readyz', status: '503' }); res.status(503).type('text/plain').send('warming\n');
});
app.all('/infer', async (_req, res) => {
  const endHttp = httpDuration.startTimer({ endpoint: 'infer' });
  const endProcessing = processingDuration.startTimer({ endpoint: 'infer' });
  if (!ready()) {
    endHttp(); endProcessing(); failuresTotal.inc({ endpoint: 'infer' }); requestsTotal.inc({ endpoint: 'infer', status: '503' });
    res.status(503).json({ status: 'not_ready', workload: workloadName }); return;
  }
  await sleep(sleepMs);
  endHttp(); endProcessing(); requestsTotal.inc({ endpoint: 'infer', status: '200' });
  res.json({ status: 'ok', workload: workloadName, version, sleep_ms: sleepMs });
});
app.get('/version', (_req, res) => { requestsTotal.inc({ endpoint: 'version', status: '200' }); res.json({ workload: workloadName, class: workloadClass, kind: workloadKind, version }); });
app.get('/metrics', async (_req, res) => { ready(); res.set('Content-Type', register.contentType); res.end(await register.metrics()); });
app.listen(port, () => console.log(`${workloadName} listening on ${port}`));

// Campaign launch + fake-call soak (target: 200 concurrent calls, docs/IMPLEMENTATION_PLAN.md §1 #8).
// Runs ONLY against the fake providers: setup() refuses unless FAKE_PROVIDERS_CONFIRMED=yes, because a
// launch dials every contact in the segment.
//
// setup:    creates a call campaign for SEGMENT_ID with AGENT_ID (demo defaults), max_concurrency =
//           TARGET_CONCURRENCY, a 24x7 window, then launches it with an Idempotency-Key (and replays the
//           launch once to prove it is idempotent).
// default:  observers poll /api/v1/calls/live and the campaign report for SOAK (default 30m), recording
//           live-call concurrency and dispatch progress.
// teardown: cancels the campaign (stops dispatch; queued calls are canceled by call-service).
//   k6 run -e TOKEN=... -e FAKE_PROVIDERS_CONFIRMED=yes tests/load/campaign_soak.js
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Counter, Gauge, Trend } from "k6/metrics";
import { config, expectStatus, headers, idempotencyKey, requireToken } from "./lib/common.js";

const TARGET = Number(__ENV.TARGET_CONCURRENCY || 200);
const liveCalls = new Gauge("nexus_live_calls");
const liveCallsTrend = new Trend("nexus_live_calls_trend");
const overCap = new Counter("nexus_concurrency_over_cap");
const dispatched = new Gauge("nexus_campaign_dispatched");

export const options = {
  scenarios: {
    observe: { executor: "constant-vus", vus: 2, duration: __ENV.SOAK || "30m" },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{endpoint:calls_live}": ["p(95)<500"],
    // The platform must never exceed the campaign/tenant concurrency cap.
    nexus_concurrency_over_cap: ["count==0"],
    // And it should actually reach the target (90% of it) at some point during the soak.
    nexus_live_calls_trend: [`max>=${Math.floor(TARGET * 0.9)}`],
    checks: ["rate>0.99"],
  },
  setupTimeout: "2m",
  teardownTimeout: "2m",
};

const cfg = config();

export function setup() {
  requireToken(cfg);
  if (__ENV.FAKE_PROVIDERS_CONFIRMED !== "yes") {
    fail("set FAKE_PROVIDERS_CONFIRMED=yes after checking the target runs PROVIDER_MODE fake (README)");
  }
  const agentId = __ENV.AGENT_ID || cfg.demo.voiceAgent;
  const segmentId = __ENV.SEGMENT_ID || cfg.demo.segment;
  if (!agentId || !segmentId) fail("AGENT_ID and SEGMENT_ID are required for a non-demo tenant");

  const body = JSON.stringify({
    name: `k6 soak ${new Date().toISOString()}`,
    description: "load test (tests/load/campaign_soak.js)",
    agent_id: agentId,
    segment_id: segmentId,
    timezone: "UTC",
    window_start: "00:00",
    window_end: "23:59",
    window_days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    max_concurrency: TARGET,
    calls_per_minute: Number(__ENV.CALLS_PER_MINUTE || 600),
    max_attempts: 1,
  });
  const created = http.post(`${cfg.baseUrl}/api/v1/campaigns`, body,
    headers(cfg, { "Idempotency-Key": idempotencyKey("campaign-create") }));
  if (!expectStatus(created, [201], "create campaign")) fail(`create failed: HTTP ${created.status}`);
  const campaign = created.json();

  const launchKey = idempotencyKey("campaign-launch");
  const launch = () =>
    http.post(`${cfg.baseUrl}/api/v1/campaigns/${campaign.id}/launch`, JSON.stringify({}),
      headers(cfg, { "Idempotency-Key": launchKey }));
  const first = launch();
  if (!expectStatus(first, [202], "launch")) fail(`launch failed: HTTP ${first.status}`);
  const replay = launch();
  check(replay, { "launch replay is idempotent": (r) => r.status === 202 && r.json("id") === campaign.id });
  return { campaignId: campaign.id };
}

export default function (data) {
  // Count live calls across pages (limit max 200 per page).
  let n = 0;
  let cursor = null;
  let ok = true;
  for (let page = 0; page < 10; page++) {
    const p = headers(cfg);
    p.tags = { endpoint: "calls_live" };
    const q = cursor ? `?limit=200&cursor=${encodeURIComponent(cursor)}` : "?limit=200";
    const res = http.get(`${cfg.baseUrl}/api/v1/calls/live${q}`, p);
    if (!expectStatus(res, [200], "calls live")) {
      ok = false;
      break;
    }
    n += (res.json("items") || []).length;
    cursor = res.json("next_cursor");
    if (!cursor) break;
  }
  if (ok) {
    liveCalls.add(n);
    liveCallsTrend.add(n);
    if (n > TARGET) overCap.add(1);
  }
  const rp = headers(cfg);
  rp.tags = { endpoint: "campaign_report" };
  const report = http.get(`${cfg.baseUrl}/api/v1/campaigns/${data.campaignId}/report`, rp);
  if (expectStatus(report, [200], "campaign report")) {
    const counts = report.json() || {};
    const total = Object.values(counts.contacts || counts.counts || {}).reduce((s, v) => s + (Number(v) || 0), 0);
    dispatched.add(total);
  }
  sleep(5);
}

export function teardown(data) {
  if (!data || !data.campaignId) return;
  const res = http.post(`${cfg.baseUrl}/api/v1/campaigns/${data.campaignId}/cancel`,
    JSON.stringify({ reason: "load test finished" }), headers(cfg));
  expectStatus(res, [200, 409], "cancel campaign");
}

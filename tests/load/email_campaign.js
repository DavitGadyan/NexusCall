// Email campaign profile (target: 100,000 emails/day = ~70/min average; this profile drives a sustained
// RATE_PER_MINUTE, default 300/min = 5x the daily average, for the burst a single campaign produces).
// Runs ONLY against the fake SES simulator (FAKE_PROVIDERS_CONFIRMED=yes): every send is relayed to Mailpit.
//
// setup:    creates an email campaign for the demo sender/template/segment with max_per_minute =
//           RATE_PER_MINUTE, launches it (Idempotency-Key, replayed once).
// default:  readers poll the campaign report and /api/v1/email/usage while the senders work, and an
//           operator mix reads messages, so API latency is measured under send load.
// teardown: cancels the campaign.
//   k6 run -e TOKEN=... -e FAKE_PROVIDERS_CONFIRMED=yes tests/load/email_campaign.js
// For the daily volume, run with SOAK=24h against a segment of >=100k contactable test contacts (README).
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Gauge } from "k6/metrics";
import { config, expectStatus, headers, idempotencyKey, requireToken } from "./lib/common.js";

const RATE = Number(__ENV.RATE_PER_MINUTE || 300);
const reportedTotal = new Gauge("nexus_email_report_total");

export const options = {
  scenarios: {
    observe: { executor: "constant-vus", vus: 2, duration: __ENV.SOAK || "20m", exec: "observe" },
    operators: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.OPERATOR_RPS || 5),
      timeUnit: "1s",
      duration: __ENV.SOAK || "20m",
      preAllocatedVUs: 10,
      exec: "operators",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{endpoint:email_messages}": ["p(95)<700"],
    "http_req_duration{endpoint:campaign_report}": ["p(95)<700"],
    checks: ["rate>0.99"],
  },
  setupTimeout: "2m",
};

const cfg = config();

export function setup() {
  requireToken(cfg);
  if (__ENV.FAKE_PROVIDERS_CONFIRMED !== "yes") {
    fail("set FAKE_PROVIDERS_CONFIRMED=yes after checking the target runs PROVIDER_MODE fake (README)");
  }
  const senderId = __ENV.SENDER_ID || cfg.demo.emailSender;
  const templateId = __ENV.TEMPLATE_ID || cfg.demo.emailTemplate;
  const segmentId = __ENV.SEGMENT_ID || cfg.demo.segment;
  if (!senderId || !templateId || !segmentId) fail("SENDER_ID, TEMPLATE_ID and SEGMENT_ID are required");
  const created = http.post(
    `${cfg.baseUrl}/api/v1/email/campaigns`,
    JSON.stringify({
      name: `k6 email ${new Date().toISOString()}`,
      sender_id: senderId,
      template_id: templateId,
      segment_id: segmentId,
      timezone: "UTC",
      max_per_minute: RATE,
    }),
    headers(cfg, { "Idempotency-Key": idempotencyKey("email-campaign-create") }),
  );
  if (!expectStatus(created, [201], "create email campaign")) fail(`create failed: HTTP ${created.status}`);
  const id = created.json("id");
  const key = idempotencyKey("email-campaign-launch");
  const launch = () =>
    http.post(`${cfg.baseUrl}/api/v1/email/campaigns/${id}/launch`, JSON.stringify({}),
      headers(cfg, { "Idempotency-Key": key }));
  if (!expectStatus(launch(), [202], "launch email campaign")) fail("launch failed");
  check(launch(), { "email launch replay is idempotent": (r) => r.status === 202 && r.json("id") === id });
  return { campaignId: id };
}

export function observe(data) {
  const p = headers(cfg);
  p.tags = { endpoint: "campaign_report" };
  const res = http.get(`${cfg.baseUrl}/api/v1/email/campaigns/${data.campaignId}/report`, p);
  if (expectStatus(res, [200], "email report")) {
    const body = res.json() || {};
    const counts = body.counts || body.states || {};
    reportedTotal.add(Object.values(counts).reduce((s, v) => s + (Number(v) || 0), 0));
  }
  const u = headers(cfg);
  u.tags = { endpoint: "email_usage" };
  expectStatus(http.get(`${cfg.baseUrl}/api/v1/email/usage`, u), [200], "email usage");
  sleep(10);
}

export function operators() {
  const p = headers(cfg);
  p.tags = { endpoint: "email_messages" };
  expectStatus(http.get(`${cfg.baseUrl}/api/v1/email/messages?limit=50`, p), [200], "email messages");
}

export function teardown(data) {
  if (!data || !data.campaignId) return;
  const res = http.post(`${cfg.baseUrl}/api/v1/email/campaigns/${data.campaignId}/cancel`,
    JSON.stringify({ reason: "load test finished" }), headers(cfg));
  expectStatus(res, [200, 409], "cancel email campaign");
}

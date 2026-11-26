// Authenticated read mix: the dashboard traffic of operators and tenant users, weighted roughly like a
// working day. Ramps to READ_VUS (default 100) and holds. Use a user with read access to every module
// (e.g. owner@acme.test). Every request carries the tenant header; RLS keeps results tenant-scoped.
//   k6 run -e TOKEN=... tests/load/read_mix.js
import http from "k6/http";
import { sleep } from "k6";
import { config, expectStatus, headers, requireToken } from "./lib/common.js";

const VUS = Number(__ENV.READ_VUS || 100);

export const options = {
  scenarios: {
    reads: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m", target: VUS },
        { duration: __ENV.HOLD || "10m", target: VUS },
        { duration: "1m", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500", "p(99)<1500"],
    "http_req_duration{endpoint:analytics_overview}": ["p(95)<1500"],
    checks: ["rate>0.99"],
  },
};

const cfg = config();

// [weight, name, path]
const MIX = [
  [10, "me", "/api/v1/me"],
  [15, "contacts", "/api/v1/contacts?limit=50"],
  [10, "leads", "/api/v1/leads?limit=50"],
  [10, "campaigns", "/api/v1/campaigns?limit=50"],
  [15, "calls", "/api/v1/calls?limit=50"],
  [10, "calls_live", "/api/v1/calls/live"],
  [10, "inbox_threads", "/api/v1/email/inbox/threads?limit=50"],
  [5, "email_campaigns", "/api/v1/email/campaigns?limit=50"],
  [5, "notifications", "/api/v1/notifications?limit=20"],
  [5, "analytics_overview", "/api/v1/analytics/overview"],
  [5, "segments", "/api/v1/segments"],
];
const TOTAL = MIX.reduce((s, m) => s + m[0], 0);

function pick() {
  let r = Math.random() * TOTAL;
  for (const m of MIX) {
    r -= m[0];
    if (r <= 0) return m;
  }
  return MIX[0];
}

export function setup() {
  requireToken(cfg);
}

export default function () {
  const [, name, path] = pick();
  const p = headers(cfg);
  p.tags = { endpoint: name };
  expectStatus(http.get(`${cfg.baseUrl}${path}`, p), [200], name);
  sleep(0.5 + Math.random());
}

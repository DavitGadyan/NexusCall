// Health and latency smoke test: a few VUs for a minute. Fails the run (non-zero exit) when thresholds
// are missed, so it can gate a rollout (docs/DEPLOYMENT.md "Rollout").
//   k6 run tests/load/smoke.js                       # unauthenticated edge checks only
//   k6 run -e TOKEN=... tests/load/smoke.js          # plus authenticated reads
//   k6 run -e SERVICE_URLS=http://localhost:18000,http://localhost:18001 tests/load/smoke.js
//       (port-forwarded services: also probes /healthz and /readyz, which are not routed at the edge)
import http from "k6/http";
import { sleep } from "k6";
import { config, expectStatus, headers } from "./lib/common.js";

export const options = {
  scenarios: {
    smoke: { executor: "constant-vus", vus: Number(__ENV.VUS || 5), duration: __ENV.DURATION || "1m" },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{kind:edge}": ["p(95)<300", "p(99)<800"],
    "http_req_duration{kind:api}": ["p(95)<500", "p(99)<1500"],
    "http_req_duration{kind:health}": ["p(95)<200"],
    checks: ["rate>0.99"],
  },
};

const cfg = config();
const serviceUrls = (__ENV.SERVICE_URLS || "").split(",").filter(Boolean);

export default function () {
  expectStatus(http.get(`${cfg.baseUrl}/api/health`, { tags: { kind: "edge" } }), [200], "web health");
  // Unauthenticated API access must be rejected, never served.
  expectStatus(http.get(`${cfg.baseUrl}/api/v1/me`, { tags: { kind: "edge" } }), [401, 403], "anonymous /me");
  if (cfg.token) {
    const p = headers(cfg);
    p.tags = { kind: "api" };
    expectStatus(http.get(`${cfg.baseUrl}/api/v1/me`, p), [200], "/api/v1/me");
  }
  for (const url of serviceUrls) {
    expectStatus(http.get(`${url}/healthz`, { tags: { kind: "health" } }), [200], "healthz");
    expectStatus(http.get(`${url}/readyz`, { tags: { kind: "health" } }), [200], "readyz");
  }
  sleep(1);
}

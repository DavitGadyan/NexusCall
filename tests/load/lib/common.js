// Shared helpers for the NexusCall k6 profiles (tests/load/README.md).
//
// Parameters (k6 -e NAME=value or environment variables):
//   BASE_URL    gateway origin. Default http://localhost:47000 (the Compose Traefik edge).
//   TOKEN       bearer access token of a demo user (see README "Getting a token"). Never commit one.
//   TENANT_ID   tenant selector header. Default: the acme demo tenant.
//
// Safety: these scripts generate real load and, for the campaign profiles, real side effects. They refuse
// any non-local BASE_URL unless ALLOW_NON_LOCAL_TARGET is set to the exact acknowledgement string below,
// which is meant only for a staging environment you own that runs PROVIDER_MODE real with sandbox
// accounts. Never point them at production.
import { check, fail } from "k6";
import exec from "k6/execution";

export const ACME_TENANT = "0a000000-0000-4000-8000-000000000001";
export const GLOBEX_TENANT = "0a000000-0000-4000-8000-000000000002";
// Deterministic demo ids (nexus_core.devseed.demo_id): acme index 1, globex index 101.
export const DEMO = {
  [ACME_TENANT]: {
    voiceAgent: "c50f343e-6705-55ab-8365-105dad1fb4bd",
    segment: "f91c36f1-240b-537d-b922-1b191b9da421",
    emailSender: "532518f1-e3cf-5761-97c2-01fe98ace409",
    emailTemplate: "79f146e0-b797-547a-9833-abe543b7eed1",
  },
  [GLOBEX_TENANT]: {
    voiceAgent: "5df9b694-a3bf-53f8-b869-69372b1b9934",
    segment: "a7aee59f-c0cb-5d25-93f8-3f6905b92712",
    emailSender: "0610b7b3-0b88-5367-ba4c-f1c0cce381d4",
    emailTemplate: "a4adf019-a376-549a-a4e7-80617b8bb09c",
  },
};

const ACK = "i-own-this-non-production-target";
const LOCAL_HOSTS = /^(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal|[a-z0-9-]+\.localhost)$/;

export function config() {
  const baseUrl = (__ENV.BASE_URL || "http://localhost:47000").replace(/\/$/, "");
  const match = baseUrl.match(/^https?:\/\/([^/:]+|\[[^\]]+\])(:\d+)?$/);
  if (!match) fail(`BASE_URL must be an origin like http://localhost:47000 (got ${baseUrl})`);
  if (!LOCAL_HOSTS.test(match[1]) && __ENV.ALLOW_NON_LOCAL_TARGET !== ACK) {
    fail(`refusing non-local BASE_URL ${baseUrl}; see tests/load/lib/common.js before overriding`);
  }
  const tenantId = __ENV.TENANT_ID || ACME_TENANT;
  return { baseUrl, token: __ENV.TOKEN || "", tenantId, demo: DEMO[tenantId] || {} };
}

export function requireToken(cfg) {
  if (!cfg.token) fail("TOKEN is required for this profile (README: Getting a token)");
}

export function headers(cfg, extra = {}) {
  const h = {
    "Content-Type": "application/json",
    "X-Request-ID": `k6-${exec.vu.idInTest}-${exec.vu.iterationInScenario}-${Date.now()}`,
    ...extra,
  };
  if (cfg.token) h.Authorization = `Bearer ${cfg.token}`;
  if (cfg.tenantId) h["X-Tenant-ID"] = cfg.tenantId;
  return { headers: h };
}

export function idempotencyKey(prefix) {
  // One key per logical operation; k6 reuses it only when the request itself is retried.
  return `${prefix}-${exec.scenario.name}-${exec.vu.idInTest}-${exec.vu.iterationInScenario}-${Date.now()}`;
}

export function expectStatus(res, statuses, name) {
  const ok = check(res, { [`${name} status ${statuses.join("|")}`]: (r) => statuses.includes(r.status) });
  if (!ok && __ENV.DEBUG) console.warn(`${name}: HTTP ${res.status} ${String(res.body).slice(0, 200)}`);
  return ok;
}

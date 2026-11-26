import { describe, expect, it } from "vitest";
import {
  clientIpFrom,
  forwardRequestHeaders,
  forwardableSetCookies,
  forwardResponseHeaders,
  isAnonymousPath,
  mapUnauthorized,
  requestIdFrom,
  resolveUpstreamPath,
  sendsTenantHeader,
} from "./proxy";

describe("proxy path allowlist", () => {
  it.each([
    [["api", "v1", "me"], "/api/v1/me"],
    [["api", "v1", "platform", "tenants", "0a000000-0000-4000-8000-000000000001"], "/api/v1/platform/tenants/0a000000-0000-4000-8000-000000000001"],
    [["api", "v1", "settings", "communication"], "/api/v1/settings/communication"],
    [["api", "v1", "audit", "events"], "/api/v1/audit/events"],
    [["api", "v1", "contacts", "a b"], "/api/v1/contacts/a%20b"],
  ])("forwards %j", (segments, expected) => {
    expect(resolveUpstreamPath(segments)).toBe(expected);
  });

  it.each([
    [[]],
    [["api", "v1"]],
    [["api", "v2", "me"]],
    [["internal", "v1", "authz", "resolve"]],
    [["webhooks", "twilio", "voice"]],
    [["media", "twilio"]],
    [["api", "v1", "..", "..", "internal", "v1", "tenants"]],
    [["api", "v1", ".", "me"]],
    [["api", "v1", "", "me"]],
    [["api", "v1", "platform/../../internal"]],
    [["api", "v1", "me\\..\\internal"]],
    [["api", "v1", "me\u0000"]],
    [["api", "v1", "me\r\nX-Injected: 1"]],
    [["API", "v1", "me"]],
    [["api", "v1", "x".repeat(201)]],
  ])("rejects %j", (segments) => {
    expect(resolveUpstreamPath(segments)).toBeNull();
  });

  it("only the invitation preview and onboarding are reachable without a session", () => {
    expect(isAnonymousPath("/api/v1/invitations/preview")).toBe(true);
    expect(isAnonymousPath("/api/v1/invitations/onboard")).toBe(true);
    expect(isAnonymousPath("/api/v1/invitations/accept")).toBe(false);
    expect(isAnonymousPath("/api/v1/invitations")).toBe(false);
    expect(isAnonymousPath("/api/v1/me")).toBe(false);
  });
});

describe("request header forwarding", () => {
  const incoming = new Headers({
    cookie: "nx_sid=secret; nx_csrf=abc",
    authorization: "Bearer attacker-supplied",
    "x-tenant-id": "11111111-1111-4111-8111-111111111111",
    "x-csrf-token": "abc",
    connection: "keep-alive, x-foo",
    "keep-alive": "timeout=5",
    "transfer-encoding": "chunked",
    upgrade: "websocket",
    "proxy-authorization": "Basic x",
    te: "trailers",
    host: "app.example",
    "x-forwarded-host": "evil.example",
    "x-actor": "service:crm-service",
    accept: "application/json",
    "content-type": "application/json",
    "idempotency-key": "k-123",
    "if-match": "\"v3\"",
    "last-event-id": "42",
  });
  const ctx = { accessToken: "server-token", tenantId: "0a000000-0000-4000-8000-000000000001", requestId: "req-12345678" };

  it("strips cookies, hop-by-hop, forwarding and client-supplied identity headers", () => {
    const out = forwardRequestHeaders(incoming, "/api/v1/memberships", ctx);
    for (const name of [
      "cookie",
      "x-csrf-token",
      "connection",
      "keep-alive",
      "transfer-encoding",
      "upgrade",
      "proxy-authorization",
      "te",
      "host",
      "x-forwarded-host",
      "x-actor",
    ]) {
      expect(out.has(name), name).toBe(false);
    }
  });

  it("replaces Authorization and X-Tenant-ID with the session's values", () => {
    const out = forwardRequestHeaders(incoming, "/api/v1/memberships", ctx);
    expect(out.get("authorization")).toBe("Bearer server-token");
    expect(out.get("x-tenant-id")).toBe("0a000000-0000-4000-8000-000000000001");
    expect(out.get("x-request-id")).toBe("req-12345678");
  });

  it("keeps the content negotiation and idempotency headers", () => {
    const out = forwardRequestHeaders(incoming, "/api/v1/memberships", ctx);
    expect(out.get("accept")).toBe("application/json");
    expect(out.get("content-type")).toBe("application/json");
    expect(out.get("idempotency-key")).toBe("k-123");
    expect(out.get("if-match")).toBe("\"v3\"");
    expect(out.get("last-event-id")).toBe("42");
  });

  it("omits X-Tenant-ID on platform routes and Authorization when signed out", () => {
    expect(forwardRequestHeaders(incoming, "/api/v1/platform/tenants", ctx).has("x-tenant-id")).toBe(false);
    const anon = forwardRequestHeaders(incoming, "/api/v1/invitations/preview", { ...ctx, accessToken: null, tenantId: null });
    expect(anon.has("authorization")).toBe(false);
    expect(anon.has("x-tenant-id")).toBe(false);
    expect(sendsTenantHeader("/api/v1/platformish")).toBe(true);
  });

  it("generates a request id unless a safe one was supplied", () => {
    expect(requestIdFrom(new Headers({ "x-request-id": "edge-req-0001" }))).toBe("edge-req-0001");
    expect(requestIdFrom(new Headers({ "x-request-id": "bad id\n" }))).toMatch(/^[0-9a-f-]{36}$/);
    expect(requestIdFrom(new Headers())).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("parses the client IP conservatively", () => {
    expect(clientIpFrom(new Headers({ "x-forwarded-for": "203.0.113.7, 10.0.0.2" }))).toBe("203.0.113.7");
    expect(clientIpFrom(new Headers({ "x-forwarded-for": "<script>" }))).toBeNull();
  });
});

describe("response header forwarding", () => {
  const upstream = new Headers({
    "content-type": "application/problem+json",
    "set-cookie": "evil=1; Path=/",
    "content-encoding": "gzip",
    "content-length": "12",
    connection: "close",
    "transfer-encoding": "chunked",
    server: "uvicorn",
    location: "http://tenant-service:8000/internal",
    "x-request-id": "req-12345678",
    "content-disposition": "attachment; filename=report.csv",
  });

  it("never lets services set cookies or leak internals", () => {
    const out = forwardResponseHeaders(upstream, { stream: false });
    for (const name of ["set-cookie", "content-encoding", "content-length", "connection", "transfer-encoding", "server", "location"]) {
      expect(out.has(name), name).toBe(false);
    }
    expect(out.get("content-type")).toBe("application/problem+json");
    expect(out.get("content-disposition")).toBe("attachment; filename=report.csv");
    expect(out.get("cache-control")).toBe("private, no-store");
  });

  it("marks event streams as unbuffered and untransformed", () => {
    const out = forwardResponseHeaders(new Headers({ "content-type": "text/event-stream" }), { stream: true });
    expect(out.get("cache-control")).toBe("no-cache, no-transform");
    expect(out.get("x-accel-buffering")).toBe("no");
  });
});

describe("401 mapping", () => {
  it("turns step_up_required into a step-up instruction", () => {
    const { body } = mapUnauthorized({ code: "step_up_required", detail: "Re-authenticate", required_acr: "aal2" });
    expect(body.code).toBe("step_up_required");
    expect(body.required_acr).toBe("aal2");
    expect(body.step_up).toEqual({ login_url: "/api/auth/login?stepup=1" });
  });

  it("treats any other 401 as an ended session without echoing upstream detail", () => {
    const { body } = mapUnauthorized({ code: "unauthorized", detail: "Token expired" });
    expect(body.code).toBe("session_expired");
    expect(body.detail).not.toContain("Token");
  });
});

describe("forwardableSetCookies", () => {
  const start = "/api/v1/integrations/oauth/google/start";
  const good = "nx_oauth_bind=Abc123_def456-ghi789jklmno; HttpOnly; Max-Age=600; Path=/oauth/callback; SameSite=lax; Secure";
  const headersWith = (...cookies: string[]) => {
    const h = new Headers();
    for (const c of cookies) h.append("set-cookie", c);
    return h;
  };

  it("forwards only the OAuth binding cookie from the OAuth start endpoint", () => {
    expect(forwardableSetCookies(start, headersWith(good))).toEqual([good]);
  });
  it("drops every cookie on other paths", () => {
    expect(forwardableSetCookies("/api/v1/integrations/connections", headersWith(good))).toEqual([]);
  });
  it("drops other cookie names, even from the OAuth start endpoint", () => {
    const planted = "__Host-nx_sid=evil; HttpOnly; Path=/; SameSite=lax; Secure";
    expect(forwardableSetCookies(start, headersWith(planted, good))).toEqual([good]);
  });
  it("drops the binding cookie when it is widened or weakened", () => {
    for (const bad of [
      good.replace("Path=/oauth/callback", "Path=/"),
      good.replace("HttpOnly; ", ""),
      good.replace("SameSite=lax", "SameSite=None"),
      `${good}; Domain=example.com`,
      "nx_oauth_bind=short; HttpOnly; Path=/oauth/callback; SameSite=lax",
    ]) {
      expect(forwardableSetCookies(start, headersWith(bad))).toEqual([]);
    }
  });
});

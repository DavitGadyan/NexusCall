/**
 * Pure helpers of the `/api/bff/[...path]` gateway proxy (kept framework-free for unit tests).
 *
 * - Only `api/v1/**` is forwarded. Internal (`/internal/v1`), webhook and media routes are unreachable.
 * - Request headers are an allowlist: cookies, the browser's own Authorization / X-Tenant-ID, hop-by-hop
 *   and forwarding headers never reach services. The BFF adds Authorization, X-Tenant-ID and X-Request-ID.
 * - Response headers are an allowlist as well: services cannot set cookies on the web origin.
 */

const MAX_SEGMENT = 200;
const MAX_PATH = 2048;

/** Endpoints a signed-out browser may call (invitation preview / onboarding, build.md §18.3). */
export const ANONYMOUS_PATHS: ReadonlySet<string> = new Set(["api/v1/invitations/preview", "api/v1/invitations/onboard"]);

const FORWARD_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "idempotency-key",
  "if-match",
  "if-none-match",
  "last-event-id",
] as const;

const FORWARD_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "content-language",
  "etag",
  "last-modified",
  "retry-after",
  "x-request-id",
] as const;

const SAFE_REQUEST_ID = /^[A-Za-z0-9._:-]{8,128}$/;

/** Returns the upstream path (`/api/v1/...`) for already-decoded route segments, or null if not allowed. */
export function resolveUpstreamPath(segments: readonly string[] | undefined): string | null {
  if (!segments || segments.length < 3) return null;
  if (segments[0] !== "api" || segments[1] !== "v1") return null;
  for (const s of segments) {
    if (s.length === 0 || s.length > MAX_SEGMENT || s === "." || s === "..") return null;
    // A decoded "/" or "\" would change the path structure upstream; control chars are never valid.
    if (/[/\\\u0000-\u001f\u007f]/.test(s)) return null;
  }
  const path = "/" + segments.map((s) => encodeURIComponent(s)).join("/");
  return path.length <= MAX_PATH ? path : null;
}

export function isAnonymousPath(upstreamPath: string): boolean {
  return ANONYMOUS_PATHS.has(upstreamPath.slice(1));
}

/** Platform-console calls never carry a tenant selector (a suspended tenant must not block them). */
export function sendsTenantHeader(upstreamPath: string): boolean {
  return !(upstreamPath === "/api/v1/platform" || upstreamPath.startsWith("/api/v1/platform/"));
}

export function requestIdFrom(incoming: Headers): string {
  const rid = incoming.get("x-request-id");
  return rid && SAFE_REQUEST_ID.test(rid) ? rid : crypto.randomUUID();
}

export type ForwardContext = {
  accessToken: string | null;
  tenantId: string | null;
  requestId: string;
  clientIp?: string | null;
};

export function forwardRequestHeaders(incoming: Headers, upstreamPath: string, ctx: ForwardContext): Headers {
  const out = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value !== null && value.length <= 1024) out.set(name, value);
  }
  if (ctx.accessToken) out.set("authorization", `Bearer ${ctx.accessToken}`);
  if (ctx.tenantId && sendsTenantHeader(upstreamPath)) out.set("x-tenant-id", ctx.tenantId);
  out.set("x-request-id", ctx.requestId);
  if (ctx.clientIp) out.set("x-forwarded-for", ctx.clientIp);
  return out;
}

export function forwardResponseHeaders(upstream: Headers, opts: { stream: boolean }): Headers {
  const out = new Headers();
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.get(name);
    if (value !== null) out.set(name, value);
  }
  if (opts.stream) {
    out.set("cache-control", "no-cache, no-transform");
    out.set("x-accel-buffering", "no");
  } else {
    out.set("cache-control", "private, no-store");
  }
  out.set("x-content-type-options", "nosniff");
  return out;
}

/** First hop of X-Forwarded-For as set by the edge router, when it looks like an IP address. */
export function clientIpFrom(incoming: Headers): string | null {
  const first = incoming.get("x-forwarded-for")?.split(",")[0]?.trim();
  return first && /^[0-9a-fA-F:.]{2,45}$/.test(first) ? first : null;
}

export type ProblemBody = { code?: unknown; [k: string]: unknown };

export const STEP_UP_LOGIN_PATH = "/api/auth/login?stepup=1";

/** 401 `step_up_required` keeps its detail and gains the BFF login URL the client turns into a step-up. */
export function mapUnauthorized(body: ProblemBody | null): { body: Record<string, unknown> } {
  if (body && body.code === "step_up_required") {
    return { body: { ...body, step_up: { login_url: STEP_UP_LOGIN_PATH } } };
  }
  return {
    body: {
      type: "https://errors.nexuscall.dev/session_expired",
      title: "Session expired",
      status: 401,
      code: "session_expired",
      detail: "Your session has ended. Sign in again.",
    },
  };
}

export function isEventStream(headers: Headers): boolean {
  return (headers.get("accept") ?? "").includes("text/event-stream");
}

/**
 * Upstream `Set-Cookie` headers are dropped, with one exception: the OAuth browser-binding cookie that
 * integration-service sets on `GET /api/v1/integrations/oauth/{provider}/start`. It is forwarded only
 * when it is exactly that cookie, HttpOnly, SameSite=Lax and scoped to `Path=/oauth/callback`, so a
 * service can never plant other cookies (session, CSRF) on the web origin.
 */
const OAUTH_START_PATH = /^\/api\/v1\/integrations\/oauth\/[a-z0-9_-]{1,40}\/start$/;
const OAUTH_BIND_COOKIE = "nx_oauth_bind";

export function forwardableSetCookies(upstreamPath: string, upstream: Headers): string[] {
  if (!OAUTH_START_PATH.test(upstreamPath)) return [];
  const all = typeof upstream.getSetCookie === "function" ? upstream.getSetCookie() : [];
  return all.filter((raw) => {
    const [pair = "", ...attrs] = raw.split(";").map((p) => p.trim());
    const eq = pair.indexOf("=");
    if (eq <= 0 || pair.slice(0, eq) !== OAUTH_BIND_COOKIE) return false;
    const value = pair.slice(eq + 1);
    if (!/^[A-Za-z0-9_-]{20,200}$/.test(value)) return false;
    const lower = attrs.map((a) => a.toLowerCase());
    if (lower.some((a) => a.startsWith("domain="))) return false;
    return lower.includes("httponly") && lower.includes("path=/oauth/callback") && lower.includes("samesite=lax");
  });
}

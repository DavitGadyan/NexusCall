/**
 * /api/bff/api/v1/** — the browser's only door to the services. Tokens never reach the browser:
 * the BFF adds Authorization, X-Tenant-ID (from the session) and X-Request-ID, strips everything else,
 * streams SSE through and turns `step_up_required` into a step-up instruction.
 */
import { type NextRequest, NextResponse } from "next/server";
import { cookieNames, CSRF_HEADER } from "@/lib/server/cookies";
import { checkMutatingRequest, isMutating, isSameOriginRequest } from "@/lib/server/csrf";
import { config } from "@/lib/server/env";
import { problemResponse } from "@/lib/server/http";
import { refresher } from "@/lib/server/oidc";
import {
  clientIpFrom,
  forwardRequestHeaders,
  forwardResponseHeaders,
  isAnonymousPath,
  isEventStream,
  mapUnauthorized,
  type ProblemBody,
  requestIdFrom,
  resolveUpstreamPath,
} from "@/lib/server/proxy";
import { ensureFreshSession } from "@/lib/server/refresh";
import type { SessionData } from "@/lib/server/session";
import { sessions } from "@/lib/server/runtime";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MAX_BODY_BYTES = 10 * 1024 * 1024;
/** File uploads: a 10 MB file plus multipart framing. Matches Traefik's body-limit-uploads (11 MiB). */
const MAX_MULTIPART_BYTES = 11 * 1024 * 1024;
const TIMEOUT_MS = 30_000;
const MAX_QUERY = 4096;

type Ctx = { params: Promise<{ path?: string[] }> };

async function handle(req: NextRequest, ctx: Ctx): Promise<Response> {
  const cfg = config();
  const names = cookieNames(cfg.env);
  const { path } = await ctx.params;
  const upstreamPath = resolveUpstreamPath(path);
  const search = req.nextUrl.search;
  if (!upstreamPath || search.length > MAX_QUERY) return problemResponse(404, "not_found", "Unknown API path.");

  const anonymous = isAnonymousPath(upstreamPath);
  const mutating = isMutating(req.method);

  let session: SessionData | null = null;
  const sid = req.cookies.get(names.session)?.value;
  if (sid && !anonymous) {
    const mgr = sessions();
    const loaded = await mgr.load(sid);
    if (loaded) {
      try {
        session = await ensureFreshSession(mgr, sid, loaded, refresher(cfg, loaded.sub));
      } catch {
        return problemResponse(503, "idp_unavailable", "Sign-in service is unavailable. Try again shortly.");
      }
    }
  }
  if (!session && !anonymous) return problemResponse(401, "session_expired", "Your session has ended. Sign in again.");

  if (mutating) {
    if (session) {
      const failure = checkMutatingRequest(req.headers, cfg.publicOrigin, {
        headerToken: req.headers.get(CSRF_HEADER),
        cookieToken: req.cookies.get(names.csrf)?.value,
        sessionToken: session.csrf,
      });
      if (failure) return problemResponse(403, failure, "The request could not be verified. Reload the page and try again.");
    } else if (!isSameOriginRequest(req.headers, cfg.publicOrigin)) {
      return problemResponse(403, "cross_origin", "Cross-origin request refused.");
    }
  }

  let body: ArrayBuffer | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    const multipart = (req.headers.get("content-type") ?? "").toLowerCase().startsWith("multipart/form-data");
    const limit = multipart ? MAX_MULTIPART_BYTES : MAX_BODY_BYTES;
    const tooLarge = () => problemResponse(413, "payload_too_large", "Request body exceeds 10 MB.");
    const declared = Number(req.headers.get("content-length") ?? "0");
    if (declared > limit) return tooLarge();
    body = await req.arrayBuffer();
    if (body.byteLength > limit) return tooLarge();
  }

  const stream = req.method === "GET" && isEventStream(req.headers);
  const requestId = requestIdFrom(req.headers);
  const headers = forwardRequestHeaders(req.headers, upstreamPath, {
    accessToken: session?.accessToken ?? null,
    tenantId: session?.tenantId ?? null,
    requestId,
    clientIp: clientIpFrom(req.headers),
  });

  let upstream: Response;
  try {
    upstream = await fetch(`${cfg.apiInternalUrl}${upstreamPath}${search}`, {
      method: req.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: stream ? req.signal : AbortSignal.any([req.signal, AbortSignal.timeout(TIMEOUT_MS)]),
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return problemResponse(timedOut ? 504 : 502, timedOut ? "upstream_timeout" : "upstream_unavailable", "The service did not respond. Try again shortly.", {
      request_id: requestId,
    });
  }

  if (upstream.status === 401) {
    let problem: ProblemBody | null = null;
    try {
      problem = (await upstream.json()) as ProblemBody;
    } catch {
      problem = null;
    }
    const mapped = mapUnauthorized(problem);
    return NextResponse.json(mapped.body, {
      status: 401,
      headers: { "content-type": "application/problem+json", "cache-control": "private, no-store", "x-request-id": requestId },
    });
  }
  if (upstream.status >= 300 && upstream.status < 400 && upstream.status !== 304) {
    // Never follow or expose internal redirects.
    return problemResponse(502, "upstream_redirect", "Unexpected response from the service.", { request_id: requestId });
  }

  const out = forwardResponseHeaders(upstream.headers, { stream });
  if (!out.has("x-request-id")) out.set("x-request-id", requestId);
  return new Response(upstream.status === 204 || upstream.status === 304 ? null : upstream.body, {
    status: upstream.status,
    headers: out,
  });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;

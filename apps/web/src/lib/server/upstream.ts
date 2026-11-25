/**
 * Server-side data helper for React Server Components and route handlers: calls the internal gateway with
 * the session's access token. Never import from client components.
 */
import { type Problem, toProblem } from "@/lib/problem";
import { config } from "./env";
import { sendsTenantHeader } from "./proxy";
import { activeSession } from "./runtime";

export type UpstreamResult<T> = { ok: true; status: number; data: T } | { ok: false; status: number; problem: Problem };

export type UpstreamInit = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  accessToken: string | null;
  tenantId?: string | null;
  timeoutMs?: number;
  clientIp?: string | null;
};

export async function upstreamFetch<T>(path: string, init: UpstreamInit): Promise<UpstreamResult<T>> {
  if (!path.startsWith("/api/v1/")) throw new Error("upstreamFetch only reaches /api/v1/*");
  const headers = new Headers({ accept: "application/json", "x-request-id": crypto.randomUUID() });
  if (init.accessToken) headers.set("authorization", `Bearer ${init.accessToken}`);
  if (init.tenantId && sendsTenantHeader(path.split("?")[0] ?? path)) headers.set("x-tenant-id", init.tenantId);
  if (init.clientIp) headers.set("x-forwarded-for", init.clientIp);
  if (init.body !== undefined) headers.set("content-type", "application/json");
  let resp: Response;
  try {
    resp = await fetch(`${config().apiInternalUrl}${path}`, {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(init.timeoutMs ?? 10_000),
    });
  } catch {
    return { ok: false, status: 503, problem: toProblem(503, { code: "unavailable" }) };
  }
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (resp.ok) return { ok: true, status: resp.status, data: body as T };
  return { ok: false, status: resp.status, problem: toProblem(resp.status, body) };
}

/** RSC convenience: uses the current session, including its selected tenant. */
export async function serverApi<T>(path: string, opts: { tenant?: boolean } = {}): Promise<UpstreamResult<T>> {
  const active = await activeSession();
  if (!active) return { ok: false, status: 401, problem: toProblem(401, { code: "session_expired" }) };
  return upstreamFetch<T>(path, {
    accessToken: active.session.accessToken,
    tenantId: opts.tenant === false ? null : active.session.tenantId,
  });
}

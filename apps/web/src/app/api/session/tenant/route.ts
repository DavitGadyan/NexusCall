/**
 * POST /api/session/tenant {tenant_id} — switches the session's tenant. CSRF-protected; the tenant must be
 * one of the user's active tenants according to tenant-service (`/api/v1/me`).
 */
import { type NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import type { Me } from "@/lib/types";
import { cookieNames, CSRF_HEADER } from "@/lib/server/cookies";
import { checkMutatingRequest } from "@/lib/server/csrf";
import { config } from "@/lib/server/env";
import { noStore, problemResponse } from "@/lib/server/http";
import { refresher } from "@/lib/server/oidc";
import { ensureFreshSession } from "@/lib/server/refresh";
import { sessions } from "@/lib/server/runtime";
import { upstreamFetch } from "@/lib/server/upstream";

export const dynamic = "force-dynamic";

const body = z.object({ tenant_id: z.string().uuid() }).strict();

export async function POST(req: NextRequest) {
  const cfg = config();
  const names = cookieNames(cfg.env);
  const mgr = sessions();
  const sid = req.cookies.get(names.session)?.value;
  const loaded = sid ? await mgr.load(sid) : null;
  if (!sid || !loaded) return problemResponse(401, "session_expired", "Sign in again.");
  const failure = checkMutatingRequest(req.headers, cfg.publicOrigin, {
    headerToken: req.headers.get(CSRF_HEADER),
    cookieToken: req.cookies.get(names.csrf)?.value,
    sessionToken: loaded.csrf,
  });
  if (failure) return problemResponse(403, failure, "The request could not be verified.");

  let parsed: z.infer<typeof body>;
  try {
    parsed = body.parse(await req.json());
  } catch {
    return problemResponse(422, "validation_error", "tenant_id must be a UUID.");
  }
  const session = await ensureFreshSession(mgr, sid, loaded, refresher(cfg, loaded.sub));
  if (!session) return problemResponse(401, "session_expired", "Sign in again.");

  const me = await upstreamFetch<Me>("/api/v1/me", { accessToken: session.accessToken });
  if (!me.ok) return problemResponse(me.status === 401 ? 401 : 503, me.problem.code, me.problem.detail);
  const target = me.data.tenants.find((t) => t.id === parsed.tenant_id);
  // Same answer whether the tenant exists or not: no enumeration.
  if (!target) return problemResponse(403, "forbidden", "You are not a member of this workspace.");

  await mgr.save(sid, { ...session, tenantId: target.id, tenantCheckedAt: Date.now() });
  return noStore(NextResponse.json({ tenant_id: target.id, name: target.name }));
}

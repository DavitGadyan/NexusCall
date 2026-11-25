/**
 * POST /api/auth/logout — CSRF-protected. Ends the Keycloak session at end_session (back channel),
 * then destroys the local session and clears cookies.
 * Accepts the CSRF token as `X-CSRF-Token` (fetch) or as the `csrf_token` form field (plain form post).
 */
import { type NextRequest, NextResponse } from "next/server";
import { safeReturnTo } from "@/lib/security/return-to";
import { cookieNames, CSRF_HEADER } from "@/lib/server/cookies";
import { checkMutatingRequest, isSameOriginRequest } from "@/lib/server/csrf";
import { config } from "@/lib/server/env";
import { clearSessionCookies, noStore, problemResponse } from "@/lib/server/http";
import { endSession } from "@/lib/server/oidc";
import { sessions } from "@/lib/server/runtime";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const cfg = config();
  const names = cookieNames(cfg.env);
  let formToken: string | null = null;
  let returnTo: string | null = null;
  const type = req.headers.get("content-type") ?? "";
  if (type.startsWith("application/x-www-form-urlencoded") || type.startsWith("multipart/form-data")) {
    const form = await req.formData();
    const t = form.get("csrf_token");
    const r = form.get("returnTo");
    formToken = typeof t === "string" ? t : null;
    returnTo = typeof r === "string" ? r : null;
  }

  const mgr = sessions();
  const sid = req.cookies.get(names.session)?.value;
  const session = sid ? await mgr.peek(sid) : null;
  if (sid && session) {
    const failure = checkMutatingRequest(req.headers, cfg.publicOrigin, {
      headerToken: req.headers.get(CSRF_HEADER) ?? formToken,
      cookieToken: req.cookies.get(names.csrf)?.value,
      sessionToken: session.csrf,
    });
    if (failure) return problemResponse(403, failure, "The sign-out request could not be verified.");
    await endSession(cfg, session.refreshToken);
    await mgr.destroy(sid);
  } else if (!isSameOriginRequest(req.headers, cfg.publicOrigin)) {
    return problemResponse(403, "cross_origin", "Cross-origin request refused.");
  }

  const wantsJson = (req.headers.get("accept") ?? "").includes("application/json");
  const res = wantsJson
    ? NextResponse.json({ signed_out: true })
    : NextResponse.redirect(new URL(safeReturnTo(returnTo, "/?signed_out=1"), cfg.publicBaseUrl), 303);
  clearSessionCookies(res, cfg.env);
  return noStore(res);
}

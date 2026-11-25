/**
 * GET /api/auth/callback — exchanges the code at the internal token endpoint, verifies the ID token
 * (issuer, audience, nonce, signature) and creates a fresh session (new id on every login: no fixation).
 */
import { type NextRequest, NextResponse } from "next/server";
import { defaultLanding } from "@/lib/access";
import type { Me } from "@/lib/types";
import { openAuthTx } from "@/lib/server/auth-tx";
import { cookieNames } from "@/lib/server/cookies";
import { config } from "@/lib/server/env";
import { clearAuthTxCookie, noStore, setSessionCookies } from "@/lib/server/http";
import { exchangeCode, verifyIdToken } from "@/lib/server/oidc";
import { sessions } from "@/lib/server/runtime";
import { chooseTenant } from "@/lib/server/shell";
import { upstreamFetch } from "@/lib/server/upstream";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const cfg = config();
  const names = cookieNames(cfg.env);
  const params = req.nextUrl.searchParams;

  const fail = (reason: string) => {
    const res = NextResponse.redirect(new URL(`/?error=${reason}`, cfg.publicBaseUrl), 303);
    clearAuthTxCookie(res, cfg.env);
    return noStore(res);
  };

  const idpError = params.get("error");
  if (idpError) return fail(idpError === "access_denied" ? "signin_cancelled" : "signin_failed");

  const tx = openAuthTx(cfg.sessionKey, req.cookies.get(names.authTx)?.value, params.get("state"));
  const code = params.get("code");
  if (!tx || !code || code.length > 4096) return fail("signin_expired");

  let tokens: Awaited<ReturnType<typeof exchangeCode>>;
  let claims: Awaited<ReturnType<typeof verifyIdToken>>;
  try {
    tokens = await exchangeCode(cfg, code, tx.verifier);
    claims = await verifyIdToken(cfg, tokens.id_token, { nonce: tx.nonce });
  } catch {
    return fail("signin_failed");
  }

  // JIT profile sync + tenant list. A deactivated account is refused here.
  const me = await upstreamFetch<Me>("/api/v1/me", { accessToken: tokens.access_token });
  if (!me.ok && me.status === 403) return fail("account_disabled");

  const mgr = sessions();
  const previousSid = req.cookies.get(names.session)?.value;
  const previous = previousSid ? await mgr.peek(previousSid) : null;
  const carriedTenant = previous && previous.sub === claims.sub ? previous.tenantId : null;
  const tenantId = me.ok ? chooseTenant(carriedTenant, me.data.tenants) : carriedTenant;

  const now = Date.now();
  const { sid, session } = await mgr.create({
    sub: claims.sub,
    email: claims.email,
    name: claims.name,
    accessToken: tokens.access_token,
    accessExpiresAt: now + tokens.expires_in * 1000,
    refreshToken: tokens.refresh_token ?? null,
    refreshExpiresAt: tokens.refresh_expires_in ? now + tokens.refresh_expires_in * 1000 : null,
    idToken: tokens.id_token,
    acr: claims.acr,
    authTime: claims.authTime,
    tenantId,
    tenantCheckedAt: me.ok ? now : 0,
  });
  if (previousSid && previous) await mgr.destroy(previousSid);

  const landing =
    tx.returnTo ||
    defaultLanding({
      tenantCount: me.ok ? me.data.tenants.length : 0,
      platformPermissions: me.ok ? me.data.platform_permissions : [],
    });
  const res = NextResponse.redirect(new URL(landing, cfg.publicBaseUrl), 303);
  clearAuthTxCookie(res, cfg.env);
  setSessionCookies(res, cfg.env, sid, session.csrf, mgr.absoluteRemainingSeconds(session));
  return noStore(res);
}

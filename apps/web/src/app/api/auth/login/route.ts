/**
 * GET /api/auth/login — starts Authorization Code + PKCE (S256) with state and nonce.
 * `?returnTo=` must be a same-origin path; `?stepup=1` asks Keycloak for aal2 with a fresh login.
 */
import { type NextRequest, NextResponse } from "next/server";
import { safeReturnTo } from "@/lib/security/return-to";
import { type AuthTransaction, sealAuthTx } from "@/lib/server/auth-tx";
import { authTxCookieOptions, cookieNames } from "@/lib/server/cookies";
import { config } from "@/lib/server/env";
import { noStore } from "@/lib/server/http";
import { buildAuthorizeUrl, redirectUri } from "@/lib/server/oidc";
import { codeChallengeS256, createCodeVerifier, createNonce, createState } from "@/lib/server/pkce";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const cfg = config();
  const params = req.nextUrl.searchParams;
  const stepUp = params.get("stepup") === "1";
  const tx: AuthTransaction = {
    state: createState(),
    nonce: createNonce(),
    verifier: createCodeVerifier(),
    // "" means "choose the default landing page after sign-in".
    returnTo: safeReturnTo(params.get("returnTo"), ""),
    stepUp,
    createdAt: Date.now(),
  };
  const location = buildAuthorizeUrl({
    publicIssuer: cfg.publicIssuer,
    clientId: cfg.clientId,
    redirectUri: redirectUri(cfg),
    state: tx.state,
    nonce: tx.nonce,
    codeChallenge: codeChallengeS256(tx.verifier),
    stepUp,
  });
  const res = NextResponse.redirect(location, 303);
  res.cookies.set(cookieNames(cfg.env).authTx, sealAuthTx(cfg.sessionKey, tx), authTxCookieOptions(cfg.env));
  return noStore(res);
}

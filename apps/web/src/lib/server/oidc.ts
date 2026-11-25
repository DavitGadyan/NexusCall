/**
 * OIDC client for the confidential `web-bff` Keycloak client (Authorization Code + PKCE, ADR-0006).
 *
 * The browser only ever sees the public authorization endpoint. Code exchange, refresh, JWKS and logout
 * use the internal (back-channel) realm URL. ID tokens are verified with `jose`: signature (JWKS),
 * algorithm, issuer, audience, expiry and nonce.
 */
import { createRemoteJWKSet, jwtVerify, type JWTPayload } from "jose";
import { z } from "zod";
import type { ServerConfig } from "./env";
import { RefreshRejected, type TokenSet } from "./refresh";

const HTTP_TIMEOUT_MS = 8_000;

export type AuthorizeParams = {
  publicIssuer: string;
  clientId: string;
  redirectUri: string;
  state: string;
  nonce: string;
  codeChallenge: string;
  stepUp: boolean;
  loginHint?: string;
};

export function buildAuthorizeUrl(p: AuthorizeParams): string {
  const url = new URL(`${p.publicIssuer}/protocol/openid-connect/auth`);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", p.clientId);
  url.searchParams.set("redirect_uri", p.redirectUri);
  url.searchParams.set("scope", "openid profile email");
  url.searchParams.set("state", p.state);
  url.searchParams.set("nonce", p.nonce);
  url.searchParams.set("code_challenge", p.codeChallenge);
  url.searchParams.set("code_challenge_method", "S256");
  if (p.stepUp) {
    // Level-of-authentication step-up: password + OTP, freshly (build.md §20.2).
    url.searchParams.set("acr_values", "aal2");
    url.searchParams.set("max_age", "0");
  }
  if (p.loginHint) url.searchParams.set("login_hint", p.loginHint);
  return url.toString();
}

export function endpoints(cfg: ServerConfig) {
  const base = `${cfg.internalOidcBase}/protocol/openid-connect`;
  return { token: `${base}/token`, jwks: `${base}/certs`, endSession: `${base}/logout`, revoke: `${base}/revoke` };
}

export function redirectUri(cfg: ServerConfig): string {
  return `${cfg.publicBaseUrl}/api/auth/callback`;
}

const tokenResponse = z.object({
  access_token: z.string().min(1),
  expires_in: z.number().int().positive(),
  refresh_token: z.string().min(1).optional(),
  refresh_expires_in: z.number().int().nonnegative().optional(),
  id_token: z.string().min(1).optional(),
  token_type: z.string(),
});

export type IdClaims = {
  sub: string;
  email: string | null;
  name: string | null;
  acr: string | null;
  authTime: number | null;
};

let jwks: { url: string; set: ReturnType<typeof createRemoteJWKSet> } | null = null;

function keySet(cfg: ServerConfig) {
  const url = endpoints(cfg).jwks;
  if (!jwks || jwks.url !== url) {
    jwks = { url, set: createRemoteJWKSet(new URL(url), { timeoutDuration: 5_000, cooldownDuration: 30_000 }) };
  }
  return jwks.set;
}

export async function verifyIdToken(
  cfg: ServerConfig,
  idToken: string,
  expected: { nonce?: string; sub?: string },
): Promise<IdClaims> {
  const { payload } = await jwtVerify(idToken, keySet(cfg), {
    issuer: cfg.publicIssuer,
    audience: cfg.clientId,
    algorithms: ["RS256", "PS256", "ES256"],
    clockTolerance: 30,
    requiredClaims: ["sub", "exp", "iat"],
  });
  return checkIdClaims(payload, cfg.clientId, expected);
}

/** Claim checks beyond signature/iss/aud/exp (exported for tests). */
export function checkIdClaims(payload: JWTPayload, clientId: string, expected: { nonce?: string; sub?: string }): IdClaims {
  if (expected.nonce !== undefined && payload.nonce !== expected.nonce) throw new Error("ID token nonce mismatch");
  if (expected.sub !== undefined && payload.sub !== expected.sub) throw new Error("ID token subject changed");
  const aud = payload.aud;
  if (Array.isArray(aud) && aud.length > 1 && payload.azp !== clientId) throw new Error("ID token azp mismatch");
  if (typeof payload.sub !== "string" || payload.sub.length === 0) throw new Error("ID token without subject");
  const str = (v: unknown) => (typeof v === "string" && v.length > 0 ? v : null);
  return {
    sub: payload.sub,
    email: str(payload.email),
    name: str(payload.name) ?? str(payload.preferred_username),
    acr: str(payload.acr),
    authTime: typeof payload.auth_time === "number" ? payload.auth_time : null,
  };
}

async function postForm(url: string, form: Record<string, string>): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams(form).toString(),
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
  });
}

export async function exchangeCode(cfg: ServerConfig, code: string, codeVerifier: string) {
  const resp = await postForm(endpoints(cfg).token, {
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(cfg),
    code_verifier: codeVerifier,
    client_id: cfg.clientId,
    client_secret: cfg.clientSecret,
  });
  if (!resp.ok) throw new Error(`Token exchange failed (${resp.status})`);
  const parsed = tokenResponse.safeParse(await resp.json());
  if (!parsed.success || !parsed.data.id_token) throw new Error("Token endpoint returned an unexpected response");
  if (parsed.data.token_type.toLowerCase() !== "bearer") throw new Error("Unexpected token type");
  return parsed.data as z.infer<typeof tokenResponse> & { id_token: string };
}

export function refresher(cfg: ServerConfig, sub: string) {
  return async (refreshToken: string): Promise<TokenSet> => {
    const resp = await postForm(endpoints(cfg).token, {
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: cfg.clientId,
      client_secret: cfg.clientSecret,
    });
    if (resp.status === 400 || resp.status === 401) throw new RefreshRejected();
    if (!resp.ok) throw new Error(`Token refresh failed (${resp.status})`);
    const parsed = tokenResponse.safeParse(await resp.json());
    if (!parsed.success) throw new Error("Token endpoint returned an unexpected response");
    const t = parsed.data;
    let claims: IdClaims | null = null;
    if (t.id_token) claims = await verifyIdToken(cfg, t.id_token, { sub });
    return {
      accessToken: t.access_token,
      expiresIn: t.expires_in,
      refreshToken: t.refresh_token ?? null,
      refreshExpiresIn: t.refresh_expires_in || null,
      idToken: t.id_token ?? null,
      acr: claims?.acr ?? null,
      authTime: claims?.authTime ?? null,
    };
  };
}

/**
 * Back-channel logout at the realm's end_session endpoint: a confidential-client POST with the refresh
 * token ends the Keycloak SSO session (and every token of it) without a browser round trip. Best effort:
 * the local session is destroyed regardless.
 */
export async function endSession(cfg: ServerConfig, refreshToken: string | null): Promise<boolean> {
  if (!refreshToken) return false;
  const form: Record<string, string> = {
    client_id: cfg.clientId,
    client_secret: cfg.clientSecret,
    refresh_token: refreshToken,
  };
  try {
    const resp = await postForm(endpoints(cfg).endSession, form);
    return resp.ok;
  } catch {
    return false;
  }
}

/**
 * Cookie names and flags per environment (ADR-0006).
 *
 * development / test: `nx_sid` without Secure (plain-http localhost).
 * staging / production: `__Host-` prefix, which the browser only accepts with Secure, Path=/ and no Domain.
 */
export type AppEnv = "development" | "test" | "staging" | "production";

export type CookieOptions = {
  httpOnly: boolean;
  secure: boolean;
  sameSite: "lax" | "strict";
  path: "/";
  maxAge?: number;
};

export function isLocalEnv(env: AppEnv): boolean {
  return env === "development" || env === "test";
}

export function cookieNames(env: AppEnv) {
  const prefix = isLocalEnv(env) ? "" : "__Host-";
  return {
    session: `${prefix}nx_sid`,
    csrf: `${prefix}nx_csrf`,
    authTx: `${prefix}nx_auth`,
  } as const;
}

/** Opaque session id: never readable by scripts. */
export function sessionCookieOptions(env: AppEnv, maxAgeSeconds: number): CookieOptions {
  return { httpOnly: true, secure: !isLocalEnv(env), sameSite: "lax", path: "/", maxAge: maxAgeSeconds };
}

/** Double-submit CSRF token: deliberately readable by same-origin scripts, useless cross-origin. */
export function csrfCookieOptions(env: AppEnv, maxAgeSeconds: number): CookieOptions {
  return { httpOnly: false, secure: !isLocalEnv(env), sameSite: "strict", path: "/", maxAge: maxAgeSeconds };
}

/** Login transaction (state, nonce, PKCE verifier): Lax so it survives the IdP's top-level redirect back. */
export function authTxCookieOptions(env: AppEnv): CookieOptions {
  return { httpOnly: true, secure: !isLocalEnv(env), sameSite: "lax", path: "/", maxAge: 600 };
}

export function expiredCookieOptions(env: AppEnv, httpOnly: boolean): CookieOptions {
  return { httpOnly, secure: !isLocalEnv(env), sameSite: "lax", path: "/", maxAge: 0 };
}

/** Client-side helper names (both are checked because the browser cannot see ENV). */
export const CSRF_COOKIE_CANDIDATES = ["__Host-nx_csrf", "nx_csrf"] as const;
export const CSRF_HEADER = "x-csrf-token";

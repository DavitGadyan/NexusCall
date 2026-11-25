/** Small response helpers for BFF route handlers. */
import { NextResponse } from "next/server";
import { NO_STORE_HEADERS } from "@/lib/security/csp";
import { type AppEnv, cookieNames, csrfCookieOptions, expiredCookieOptions, sessionCookieOptions } from "./cookies";

export function problemResponse(status: number, code: string, detail: string, extra: Record<string, unknown> = {}) {
  return NextResponse.json(
    { type: `https://errors.nexuscall.dev/${code}`, title: code.replace(/_/g, " "), status, code, detail, ...extra },
    { status, headers: { ...NO_STORE_HEADERS, "content-type": "application/problem+json" } },
  );
}

export function noStore<T extends Response>(res: T): T {
  for (const [k, v] of Object.entries(NO_STORE_HEADERS)) res.headers.set(k, v);
  return res;
}

export function setSessionCookies(res: NextResponse, env: AppEnv, sid: string, csrf: string, maxAgeSeconds: number) {
  const names = cookieNames(env);
  res.cookies.set(names.session, sid, sessionCookieOptions(env, maxAgeSeconds));
  res.cookies.set(names.csrf, csrf, csrfCookieOptions(env, maxAgeSeconds));
}

export function clearSessionCookies(res: NextResponse, env: AppEnv) {
  const names = cookieNames(env);
  res.cookies.set(names.session, "", expiredCookieOptions(env, true));
  res.cookies.set(names.csrf, "", { ...expiredCookieOptions(env, false), sameSite: "strict" });
}

export function clearAuthTxCookie(res: NextResponse, env: AppEnv) {
  res.cookies.set(cookieNames(env).authTx, "", expiredCookieOptions(env, true));
}

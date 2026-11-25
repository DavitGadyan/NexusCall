/**
 * Security middleware (Edge runtime):
 * - per-request CSP nonce: no inline scripts, `frame-ancestors 'none'`
 * - signed-out users are sent to sign-in for /app, /admin and /platform (layouts validate the session fully)
 * - authenticated pages are never cached
 */
import { type NextRequest, NextResponse } from "next/server";
import { buildCsp, generateNonce, isProtectedPath, NO_STORE_HEADERS, SESSION_COOKIE_NAMES } from "@/lib/security/csp";

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const https = req.headers.get("x-forwarded-proto") === "https" || req.nextUrl.protocol === "https:";

  if (isProtectedPath(pathname) && !SESSION_COOKIE_NAMES.some((name) => req.cookies.has(name))) {
    const login = new URL("/api/auth/login", req.url);
    login.searchParams.set("returnTo", `${pathname}${search}`);
    const res = NextResponse.redirect(login, 303);
    for (const [k, v] of Object.entries(NO_STORE_HEADERS)) res.headers.set(k, v);
    return res;
  }

  const nonce = generateNonce();
  const csp = buildCsp(nonce, { dev: process.env.NODE_ENV === "development", https });
  const requestHeaders = new Headers(req.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);
  requestHeaders.set("x-nexus-path", `${pathname}${search}`.slice(0, 2048));

  const res = NextResponse.next({ request: { headers: requestHeaders } });
  res.headers.set("Content-Security-Policy", csp);
  for (const [k, v] of Object.entries(NO_STORE_HEADERS)) res.headers.set(k, v);
  res.headers.set("Vary", "Cookie");
  // Invitation tokens live in the URL: never leak them through Referer.
  res.headers.set("Referrer-Policy", pathname.startsWith("/invite") ? "no-referrer" : "strict-origin-when-cross-origin");
  if (https) res.headers.set("Strict-Transport-Security", "max-age=63072000; includeSubDomains");
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|robots.txt).*)"],
};

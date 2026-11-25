/**
 * CSRF defence for every mutating BFF request: double-submit token bound to the session, plus an
 * Origin / Fetch-Metadata check as defence in depth.
 *
 * The token is random per session and stored inside the encrypted session record. The browser holds a
 * readable copy in a cookie; same-origin scripts echo it in `X-CSRF-Token`. A request is accepted only
 * when header == cookie == session value.
 */
import { safeEqual } from "./crypto";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function isMutating(method: string): boolean {
  return !SAFE_METHODS.has(method.toUpperCase());
}

export type CsrfInput = {
  headerToken: string | null | undefined;
  cookieToken: string | null | undefined;
  sessionToken: string | null | undefined;
};

export function verifyCsrfTokens({ headerToken, cookieToken, sessionToken }: CsrfInput): boolean {
  // Both comparisons always run (no short-circuit timing difference).
  const a = safeEqual(headerToken, cookieToken);
  const b = safeEqual(headerToken, sessionToken);
  return a && b;
}

/**
 * Rejects requests a browser marks as cross-site. Requests without either header (non-browser
 * clients) still need the CSRF token, which a cross-site page cannot read.
 */
export function isSameOriginRequest(headers: Headers, publicOrigin: string): boolean {
  const origin = headers.get("origin");
  if (origin !== null) return origin === publicOrigin;
  const site = headers.get("sec-fetch-site");
  if (site !== null) return site === "same-origin" || site === "none";
  return true;
}

export type CsrfFailure = "cross_origin" | "csrf_invalid";

export function checkMutatingRequest(
  headers: Headers,
  publicOrigin: string,
  tokens: CsrfInput,
): CsrfFailure | null {
  if (!isSameOriginRequest(headers, publicOrigin)) return "cross_origin";
  if (!verifyCsrfTokens(tokens)) return "csrf_invalid";
  return null;
}

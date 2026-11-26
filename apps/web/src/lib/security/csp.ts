/**
 * Content-Security-Policy and response hardening shared by the middleware (Edge runtime safe: no Node APIs).
 */
/**
 * Normalises a configured media origin (e.g. the public object-store endpoint) to `scheme://host[:port]`.
 * Anything that is not a plain http(s) origin is ignored, so a bad value can never widen the policy.
 */
export function mediaOrigin(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    if (u.username || u.password) return null;
    return u.origin;
  } catch {
    return null;
  }
}

export function buildCsp(nonce: string, opts: { dev: boolean; https: boolean; mediaOrigin?: string | null }): string {
  const scriptSrc = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"];
  // React Refresh needs eval in `next dev` only. Never in a production build.
  if (opts.dev) scriptSrc.push("'unsafe-eval'");
  const directives: string[] = [
    "default-src 'self'",
    `script-src ${scriptSrc.join(" ")}`,
    // No inline scripts. Inline *style attributes* are needed by React/Tailwind utilities.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data:",
    `connect-src 'self'${opts.dev ? " ws:" : ""}`,
    // Call recordings: same origin, or presigned URLs on the configured object-store origin only.
    `media-src 'self'${opts.mediaOrigin ? ` ${opts.mediaOrigin}` : ""}`,
    "frame-src 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
  ];
  if (opts.https) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

/** Random nonce, base64. Uses Web Crypto so it works in Edge middleware and Node. */
export function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** Pages that require a signed-in session. API routes authenticate themselves. */
export const PROTECTED_PREFIXES = ["/app", "/admin", "/platform"] as const;

export function isProtectedPath(pathname: string): boolean {
  return PROTECTED_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Both cookie names are accepted here: the Edge middleware does not depend on server env. */
export const SESSION_COOKIE_NAMES = ["__Host-nx_sid", "nx_sid"] as const;

export const NO_STORE_HEADERS: Record<string, string> = {
  "Cache-Control": "private, no-store, max-age=0, must-revalidate",
  Pragma: "no-cache",
  Expires: "0",
};

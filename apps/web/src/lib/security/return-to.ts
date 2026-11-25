/**
 * Open-redirect guard for `?returnTo=` (login, logout, step-up).
 *
 * Only same-origin, absolute *paths* are accepted. Anything that a browser could resolve to another
 * origin (scheme-relative `//host`, backslash tricks, schemes, control characters) falls back.
 */
const SENTINEL = "http://return-to.invalid";
const MAX_LENGTH = 2048;
// Paths that must never be a post-login destination (would loop or trigger side effects).
const BLOCKED_PREFIXES = ["/api/auth/", "/api/bff/", "/api/session/"];

export function safeReturnTo(raw: unknown, fallback = "/app"): string {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > MAX_LENGTH) return fallback;
  // Must be a rooted path, never scheme-relative or backslash-prefixed.
  if (!raw.startsWith("/") || raw.startsWith("//")) return fallback;
  // Browsers treat "\" like "/" in URLs; reject anywhere to avoid "/\evil.com".
  if (raw.includes("\\")) return fallback;
  // Control characters (tab/newline are silently stripped by the URL parser) and spaces.
  if (/[\u0000-\u001f\u007f\s]/.test(raw)) return fallback;
  let url: URL;
  try {
    url = new URL(raw, SENTINEL);
  } catch {
    return fallback;
  }
  if (url.origin !== SENTINEL) return fallback;
  const path = `${url.pathname}${url.search}${url.hash}`;
  if (path.startsWith("//")) return fallback;
  if (BLOCKED_PREFIXES.some((p) => url.pathname === p.slice(0, -1) || url.pathname.startsWith(p))) return fallback;
  return path;
}

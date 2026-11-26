"use client";
/**
 * Module helpers shared by the CRM, campaign and AI pages that `src/lib/client/api.ts` does not cover yet:
 * multipart uploads (CSV imports, knowledge documents), presigned download links and job polling delays.
 *
 * `uploadFile` mirrors `api()`: same BFF prefix, CSRF header, Idempotency-Key, problem+json parsing and
 * step-up / session-expired handling. It should move into `api.ts` (FormData bodies) — see the module report.
 */
import { ApiError, readCsrfToken, signInUrl, STEP_UP_EVENT } from "@/lib/client/api";
import { toProblem } from "@/lib/problem";

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

/** Client-side pre-check only; the service validates type (extension and content) and size again. */
export function validateUploadFile(
  file: { name: string; size: number },
  opts: { extensions: readonly string[]; maxBytes?: number },
): string | null {
  const max = opts.maxBytes ?? MAX_UPLOAD_BYTES;
  const name = file.name.toLowerCase();
  if (!opts.extensions.some((ext) => name.endsWith(ext))) return `Only ${opts.extensions.join(", ")} files are accepted.`;
  if (file.size === 0) return "The file is empty.";
  if (file.size > max) return `The file is larger than ${formatBytes(max)}.`;
  return null;
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export async function uploadFile<T>(
  path: string,
  file: File,
  opts: { fields?: Record<string, string>; idempotencyKey: string; signal?: AbortSignal },
): Promise<T> {
  if (!path.startsWith("/api/v1/")) throw new Error(`API paths must start with /api/v1/ (got ${path})`);
  const form = new FormData();
  form.append("file", file, file.name);
  for (const [k, v] of Object.entries(opts.fields ?? {})) form.append(k, v);
  const headers: Record<string, string> = { Accept: "application/json", "Idempotency-Key": opts.idempotencyKey };
  const token = readCsrfToken();
  if (token) headers["X-CSRF-Token"] = token;
  let resp: Response;
  try {
    // No Content-Type header: the browser sets multipart/form-data with the boundary.
    resp = await fetch(`/api/bff${path}`, { method: "POST", headers, body: form, credentials: "same-origin", cache: "no-store", signal: opts.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(toProblem(0, { code: "network_error", detail: "Network error. Check your connection and try again." }));
  }
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (resp.ok) return body as T;
  const error = new ApiError(toProblem(resp.status, body));
  if (typeof window !== "undefined") {
    if (error.isStepUp) window.dispatchEvent(new CustomEvent(STEP_UP_EVENT, { detail: { message: error.problem.detail } }));
    else if (resp.status === 401) window.location.assign(signInUrl());
  }
  throw error;
}

/**
 * Presigned object-storage URLs come from our own services, but they are still data: only http(s) URLs are
 * followed (never `javascript:` or `data:`), and they are never stored or logged by the UI.
 */
export function safeDownloadUrl(url: unknown): string | null {
  if (typeof url !== "string" || url.length > 4096) return null;
  try {
    const parsed = new URL(url, "http://invalid.local");
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    if (parsed.host === "invalid.local") return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function openDownload(url: string): boolean {
  const safe = safeDownloadUrl(url);
  if (!safe) return false;
  const a = document.createElement("a");
  a.href = safe;
  // Same tab: the presigned response is `Content-Disposition: attachment`, so the page stays put, and a
  // click after an await is not treated as a popup.
  a.rel = "noopener noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
  return true;
}

/** 409 from an optimistic-lock check (as opposed to a duplicate name or an invalid state). */
export function isVersionConflict(err: { status: number; problem: { detail: string } } | null | undefined): boolean {
  if (!err || err.status !== 409) return false;
  return /changed by someone else|reload and retry/i.test(err.problem.detail);
}

/** Poll delay for async jobs: 1.5 s, growing to 15 s; stops after ~10 minutes (returns null). */
export function nextPollDelay(attempt: number): number | null {
  if (attempt >= 60) return null;
  return Math.min(15_000, Math.round(1500 * 1.25 ** Math.max(0, attempt)));
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

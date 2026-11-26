"use client";
/**
 * Module helpers shared by the CRM, campaign and AI pages that `src/lib/client/api.ts` does not cover yet:
 * multipart uploads (CSV imports, knowledge documents), presigned download links and job polling delays.
 *
 * `uploadFile` sends multipart through the shared `api()` wrapper (CSRF, Idempotency-Key, problem+json,
 * step-up and session handling all come from there).
 */
import { api } from "@/lib/client/api";

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
  const form = new FormData();
  form.append("file", file, file.name);
  for (const [k, v] of Object.entries(opts.fields ?? {})) form.append(k, v);
  return api<T>(path, { method: "POST", body: form, idempotencyKey: opts.idempotencyKey, signal: opts.signal });
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

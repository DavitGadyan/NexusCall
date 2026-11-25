"use client";
/**
 * Browser fetch wrapper for `/api/bff/api/v1/*`. Adds the CSRF header to mutations, an Idempotency-Key
 * when asked, parses problem+json, and turns `step_up_required` / `session_expired` into UI flows.
 */
import { fieldErrors, type Problem, toProblem } from "@/lib/problem";

export const STEP_UP_EVENT = "nexus:step-up";

export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem;
  readonly fields: Record<string, string>;

  constructor(problem: Problem) {
    super(problem.detail);
    this.name = "ApiError";
    this.status = problem.status;
    this.problem = problem;
    this.fields = fieldErrors(problem);
  }

  get code() {
    return this.problem.code;
  }
  get isConflict() {
    return this.status === 409;
  }
  get isStepUp() {
    return this.status === 401 && this.problem.code === "step_up_required";
  }
  get isForbidden() {
    return this.status === 403;
  }
}

export function readCsrfToken(): string | null {
  if (typeof document === "undefined") return null;
  for (const part of document.cookie.split(";")) {
    const [rawName, ...rest] = part.trim().split("=");
    if (rawName === "__Host-nx_csrf" || rawName === "nx_csrf") return decodeURIComponent(rest.join("="));
  }
  return null;
}

export function stepUpLoginUrl(returnTo?: string): string {
  const target = returnTo ?? (typeof window !== "undefined" ? `${window.location.pathname}${window.location.search}` : "/app");
  return `/api/auth/login?stepup=1&returnTo=${encodeURIComponent(target)}`;
}

export function signInUrl(returnTo?: string): string {
  const target = returnTo ?? (typeof window !== "undefined" ? `${window.location.pathname}${window.location.search}` : "/app");
  return `/api/auth/login?returnTo=${encodeURIComponent(target)}`;
}

export type ApiOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** true generates a key; a string is used as-is (keep it stable across retries of the same action). */
  idempotencyKey?: boolean | string;
  signal?: AbortSignal;
  /** do not redirect to sign-in on an ended session (e.g. background polling) */
  quietAuth?: boolean;
};

function toBffUrl(path: string): string {
  if (!path.startsWith("/api/v1/")) throw new Error(`API paths must start with /api/v1/ (got ${path})`);
  return `/api/bff${path}`;
}

export async function api<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
  const method = opts.method ?? (opts.body === undefined ? "GET" : "POST");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const token = readCsrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  if (opts.idempotencyKey) {
    headers["Idempotency-Key"] = typeof opts.idempotencyKey === "string" ? opts.idempotencyKey : crypto.randomUUID();
  }
  let resp: Response;
  try {
    resp = await fetch(toBffUrl(path), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "same-origin",
      cache: "no-store",
      signal: opts.signal,
    });
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
    if (error.isStepUp) {
      window.dispatchEvent(new CustomEvent(STEP_UP_EVENT, { detail: { message: error.problem.detail } }));
    } else if (resp.status === 401 && !opts.quietAuth) {
      window.location.assign(signInUrl());
    }
  }
  throw error;
}

export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  return new ApiError(toProblem(0, { code: "client_error", detail: err instanceof Error ? err.message : "Unexpected error" }));
}

/** Builds `/api/v1/...?a=b` skipping empty values. */
export function withQuery(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

/**
 * Server-only configuration. Read lazily (never at build time) and validated once.
 * Error messages name the variable, never its value.
 */
import { z } from "zod";
import type { AppEnv } from "./cookies";
import { parseSessionKey } from "./crypto";

const schema = z.object({
  ENV: z.enum(["development", "test", "staging", "production"]).default("development"),
  NEXUS_PUBLIC_BASE_URL: z.string().url(),
  NEXUS_OIDC_PUBLIC_ISSUER: z.string().url(),
  NEXUS_OIDC_INTERNAL_BASE: z.string().url(),
  NEXUS_OIDC_CLIENT_ID: z.string().min(1).default("web-bff"),
  KC_WEB_BFF_CLIENT_SECRET: z.string().min(16),
  NEXUS_SESSION_KEY: z.string().regex(/^[0-9a-fA-F]{64}$/, "must be 64 hex characters"),
  REDIS_URL: z.string().min(1),
  NEXUS_API_INTERNAL_URL: z.string().url(),
  NEXUS_SESSION_IDLE_MINUTES: z.coerce.number().int().min(5).max(240).default(30),
  NEXUS_SESSION_MAX_HOURS: z.coerce.number().int().min(1).max(24).default(10),
});

export type ServerConfig = {
  env: AppEnv;
  publicBaseUrl: string;
  publicOrigin: string;
  publicIssuer: string;
  internalOidcBase: string;
  clientId: string;
  clientSecret: string;
  sessionKey: Buffer;
  redisUrl: string;
  apiInternalUrl: string;
  idleTimeoutMs: number;
  absoluteTimeoutMs: number;
};

let cached: ServerConfig | null = null;

export function loadConfig(source: Record<string, string | undefined> = process.env): ServerConfig {
  const parsed = schema.safeParse(source);
  if (!parsed.success) {
    const fields = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    throw new Error(`Web BFF configuration is invalid (${fields})`);
  }
  const e = parsed.data;
  const publicUrl = new URL(e.NEXUS_PUBLIC_BASE_URL);
  if ((e.ENV === "production" || e.ENV === "staging") && publicUrl.protocol !== "https:") {
    throw new Error("NEXUS_PUBLIC_BASE_URL must use https outside development");
  }
  return {
    env: e.ENV,
    publicBaseUrl: e.NEXUS_PUBLIC_BASE_URL.replace(/\/$/, ""),
    publicOrigin: publicUrl.origin,
    publicIssuer: e.NEXUS_OIDC_PUBLIC_ISSUER.replace(/\/$/, ""),
    internalOidcBase: e.NEXUS_OIDC_INTERNAL_BASE.replace(/\/$/, ""),
    clientId: e.NEXUS_OIDC_CLIENT_ID,
    clientSecret: e.KC_WEB_BFF_CLIENT_SECRET,
    sessionKey: parseSessionKey(e.NEXUS_SESSION_KEY),
    redisUrl: e.REDIS_URL,
    apiInternalUrl: e.NEXUS_API_INTERNAL_URL.replace(/\/$/, ""),
    idleTimeoutMs: e.NEXUS_SESSION_IDLE_MINUTES * 60_000,
    absoluteTimeoutMs: e.NEXUS_SESSION_MAX_HOURS * 3_600_000,
  };
}

export function config(): ServerConfig {
  if (!cached) cached = loadConfig();
  return cached;
}

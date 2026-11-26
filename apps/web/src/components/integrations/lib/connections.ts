/**
 * Provider-connection form logic. Credentials are write-only: the form state is the only place a value
 * exists in the browser; it is sent once and cleared. Labels and hints mirror
 * services/integration_service/integration_service/providers/catalogue.py (shape rules only, never values).
 */
import type { Problem } from "@/lib/problem";
import type { Connection, Provider } from "./types";

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export const PROVIDER_LABELS: Record<string, string> = {
  twilio: "Twilio",
  ses: "Amazon SES",
  anthropic: "Anthropic",
  deepgram: "Deepgram",
  elevenlabs: "ElevenLabs",
  voyage: "Voyage AI",
  google_calendar: "Google Calendar",
  hubspot: "HubSpot CRM",
  google_oauth_client: "Google OAuth app",
  hubspot_oauth_client: "HubSpot OAuth app",
};

export const PROVIDER_PURPOSE: Record<string, string> = {
  twilio: "Telephony for AI calls",
  ses: "Email sending and inbound mail",
  anthropic: "Language models",
  deepgram: "Speech to text",
  elevenlabs: "Text to speech",
  voyage: "Embeddings for knowledge search",
  google_calendar: "Calendar availability and bookings",
  hubspot: "CRM sync",
  google_oauth_client: "The platform's Google app registration",
  hubspot_oauth_client: "The platform's HubSpot app registration",
};

export const FIELD_LABELS: Record<string, string> = {
  account_sid: "Account SID",
  auth_token: "Auth token",
  region: "AWS region",
  access_key_id: "Access key ID",
  secret_access_key: "Secret access key",
  api_key: "API key",
  client_id: "Client ID",
  client_secret: "Client secret",
  refresh_token: "Refresh token",
  access_token: "Access token",
  calendar_id: "Calendar ID",
};

/** Shape rules, so users can fix a typo before the server rejects it (the server checks again). */
export const FIELD_HINTS: Record<string, string> = {
  account_sid: "AC followed by 32 hex characters.",
  auth_token: "32 hex characters.",
  region: "An AWS region such as us-east-1.",
  access_key_id: "AKIA or ASIA followed by 16 characters.",
  secret_access_key: "40 characters.",
  client_secret: "16–512 URL-safe characters.",
  refresh_token: "16–512 URL-safe characters.",
  access_token: "16–512 URL-safe characters.",
};

/** Identifiers that are not secrets on their own; everything else is a password-type input. */
const IDENTIFIER_FIELDS = new Set(["account_sid", "region", "access_key_id", "client_id"]);

export function isIdentifierField(name: string): boolean {
  return IDENTIFIER_FIELDS.has(name);
}

/**
 * Field names only (never values), mirrored from the integration-service catalogue. Used by the platform
 * console when the tenant-scoped provider list is not reachable without a workspace.
 */
export const FALLBACK_PROVIDERS: Provider[] = [
  { provider: "twilio", credential_fields: ["account_sid", "auth_token"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["call-service", "voice-service"] },
  { provider: "ses", credential_fields: ["access_key_id", "region", "secret_access_key"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["email-inbox-service", "email-service", "notification-service"] },
  { provider: "anthropic", credential_fields: ["api_key"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["ai-gateway"] },
  { provider: "deepgram", credential_fields: ["api_key"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["voice-service"] },
  { provider: "elevenlabs", credential_fields: ["api_key"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["voice-service"] },
  { provider: "voyage", credential_fields: ["api_key"], optional_fields: [], config_keys: [], oauth: false, platform_only: false, platform_services: ["ai-gateway"] },
  { provider: "google_oauth_client", credential_fields: ["client_id", "client_secret"], optional_fields: [], config_keys: [], oauth: false, platform_only: true, platform_services: ["integration-service"] },
  { provider: "hubspot_oauth_client", credential_fields: ["client_id", "client_secret"], optional_fields: [], config_keys: [], oauth: false, platform_only: true, platform_services: ["integration-service"] },
];

export function platformProviders(items: Provider[]): Provider[] {
  return items.filter((p) => p.platform_services.length > 0).sort((a, b) => providerLabel(a.provider).localeCompare(providerLabel(b.provider)));
}

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider.replace(/_/g, " ");
}

export function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function tenantProviders(items: Provider[]): Provider[] {
  return items.filter((p) => !p.platform_only).sort((a, b) => providerLabel(a.provider).localeCompare(providerLabel(b.provider)));
}

export type BuiltCredentials = { credentials: Record<string, string>; errors: Record<string, string> };

/**
 * Trims every value; required fields must be present, optional ones are sent only when filled in. Error keys
 * are `credentials.<field>` to line up with the server's field errors.
 */
export function buildCredentials(
  spec: Pick<Provider, "credential_fields" | "optional_fields">,
  values: Record<string, string>,
): BuiltCredentials {
  const credentials: Record<string, string> = {};
  const errors: Record<string, string> = {};
  for (const name of spec.credential_fields) {
    const v = (values[name] ?? "").trim();
    if (!v) errors[`credentials.${name}`] = "Required";
    else credentials[name] = v;
  }
  for (const name of spec.optional_fields) {
    const v = (values[name] ?? "").trim();
    if (v) credentials[name] = v;
  }
  return { credentials, errors };
}

export function buildConfig(spec: Pick<Provider, "config_keys">, values: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of spec.config_keys) {
    const v = (values[key] ?? "").trim();
    if (v) out[key] = v;
  }
  return out;
}

/**
 * The catalogue reports credential problems as `fields: [{field, rule}]` (names and rules, never values) and
 * config problems as `fields: ["key"]`. Merged with ordinary FastAPI field errors.
 */
export function credentialFieldErrors(problem: Pick<Problem, "detail"> & { fields?: unknown }, base: Record<string, string> = {}): Record<string, string> {
  const out: Record<string, string> = { ...base };
  const list = Array.isArray(problem.fields) ? problem.fields : [];
  const isConfig = /configuration/i.test(problem.detail ?? "");
  for (const item of list) {
    if (item && typeof item === "object" && typeof (item as { field?: unknown }).field === "string") {
      const { field, rule } = item as { field: string; rule?: unknown };
      const key = `credentials.${field}`;
      if (!out[key]) out[key] = typeof rule === "string" ? (rule === "required" ? "Required" : `Must be ${rule}`) : "Invalid value";
    } else if (typeof item === "string") {
      const key = `${isConfig ? "config" : "credentials"}.${item}`;
      if (!out[key]) out[key] = isConfig ? "Not a valid setting for this provider" : "Invalid value";
    }
  }
  return out;
}

const HEALTH: Record<string, { label: string; tone: Tone; help: string }> = {
  healthy: { label: "Healthy", tone: "ok", help: "Credentials are stored and the last check passed." },
  failing: { label: "Failing", tone: "danger", help: "The last test or use failed. Test again or rotate the credentials." },
  rotation_overdue: { label: "Rotation overdue", tone: "warn", help: "The rotation date has passed. Rotate the credentials." },
  vault_error: { label: "Vault error", tone: "danger", help: "The secret store could not be read or written. Platform operators are alerted." },
  disabled: { label: "Disabled", tone: "neutral", help: "The connection is kept but not used." },
  revoked: { label: "Revoked", tone: "neutral", help: "The credentials were deleted. Create a new connection to reconnect." },
  revoked_delete_pending: { label: "Delete pending", tone: "warn", help: "Revoked; the stored secret is still being deleted." },
};

export function healthInfo(health: string): { label: string; tone: Tone; help: string } {
  return HEALTH[health] ?? { label: health.replace(/_/g, " "), tone: "neutral", help: "" };
}

const DAY = 86_400_000;

export function rotationStatus(nextDue: string | null, now = Date.now()): { label: string; tone: Tone } {
  if (!nextDue) return { label: "Not scheduled", tone: "neutral" };
  const t = new Date(nextDue).getTime();
  if (Number.isNaN(t)) return { label: "Not scheduled", tone: "neutral" };
  const days = Math.ceil((t - now) / DAY);
  if (days <= 0) return { label: "Overdue", tone: "danger" };
  if (days <= 14) return { label: `Due in ${days} day${days === 1 ? "" : "s"}`, tone: "warn" };
  return { label: `Due in ${days} days`, tone: "neutral" };
}

export type ConnectionActions = { test: boolean; rotate: boolean; disable: boolean; enable: boolean; revoke: boolean };

export function connectionActions(c: Pick<Connection, "status" | "auth_type">): ConnectionActions {
  if (c.status === "revoked") return { test: false, rotate: false, disable: false, enable: false, revoke: false };
  if (c.status === "disabled") return { test: false, rotate: false, disable: false, enable: true, revoke: true };
  return { test: true, rotate: c.auth_type !== "oauth", disable: true, enable: false, revoke: true };
}

/** The typed phrase for revocation: the provider key, so it cannot be confirmed by reflex. */
export function revokePhrase(c: Pick<Connection, "provider">): string {
  return `revoke ${c.provider}`;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type OAuthReturn = { tone: "ok" | "warn" | "danger"; title: string; provider: string; connectionId: string | null };

/** Reads the OAuth callback redirect (`?oauth=<status>&provider=<key>&connection=<uuid>`). Values are untrusted. */
export function parseOAuthReturn(params: URLSearchParams): OAuthReturn | null {
  const status = params.get("oauth");
  if (!status) return null;
  const rawProvider = params.get("provider") ?? "";
  const provider = /^[a-z_]{2,32}$/.test(rawProvider) ? rawProvider : "the provider";
  const rawConn = params.get("connection");
  const connectionId = rawConn && UUID.test(rawConn) ? rawConn : null;
  const name = provider === "the provider" ? provider : providerLabel(provider);
  if (status === "active") return { tone: "ok", title: `${name} connected`, provider, connectionId };
  if (status === "denied") return { tone: "warn", title: `${name} was not connected: access was denied or the request expired`, provider, connectionId: null };
  return { tone: "danger", title: `${name} connection needs attention (${status.replace(/[^a-z_]/g, "").replace(/_/g, " ") || "error"})`, provider, connectionId };
}

/**
 * OAuth start returns the provider's authorization URL. Follow it only when it is an absolute https URL
 * without embedded credentials (never `javascript:`, `data:` or a relative path).
 */
export function safeProviderUrl(url: unknown): string | null {
  if (typeof url !== "string" || url.length > 4096) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== "https:") return null;
    if (u.username || u.password) return null;
    if (!u.hostname) return null;
    return u.toString();
  } catch {
    return null;
  }
}

/** Only the OAuth callback keys are passed to the client (values are re-validated there). */
export function oauthQueryFrom(sp: Record<string, string | string[] | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of ["oauth", "provider", "connection"]) {
    const v = sp[k];
    if (typeof v === "string" && v.length <= 64) out[k] = v;
  }
  return out;
}

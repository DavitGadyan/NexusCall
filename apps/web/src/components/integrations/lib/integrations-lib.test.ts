import { describe, expect, it } from "vitest";
import {
  buildConfig,
  buildCredentials,
  connectionActions,
  credentialFieldErrors,
  healthInfo,
  parseOAuthReturn,
  revokePhrase,
  rotationStatus,
  safeProviderUrl,
  tenantProviders,
} from "./connections";
import { canRedeliver, endpointPatch, generateSecret, validateEndpointForm, validateSecret, validateUrl } from "./webhooks";
import type { Endpoint, Provider } from "./types";

const twilio: Provider = {
  provider: "twilio",
  credential_fields: ["account_sid", "auth_token"],
  optional_fields: [],
  config_keys: [],
  oauth: false,
  platform_only: false,
  platform_services: ["call-service"],
};
const hubspot: Provider = { ...twilio, provider: "hubspot", credential_fields: ["access_token"], optional_fields: ["refresh_token"], oauth: true };

describe("credentials", () => {
  it("requires every credential field and trims values", () => {
    const out = buildCredentials(twilio, { account_sid: "  AC123 ", auth_token: "" });
    expect(out.credentials).toEqual({ account_sid: "AC123" });
    expect(out.errors).toEqual({ "credentials.auth_token": "Required" });
  });

  it("sends optional fields only when filled in", () => {
    expect(buildCredentials(hubspot, { access_token: "tok", refresh_token: " " }).credentials).toEqual({ access_token: "tok" });
    expect(buildCredentials(hubspot, { access_token: "tok", refresh_token: "r" }).credentials).toEqual({ access_token: "tok", refresh_token: "r" });
  });

  it("keeps only known config keys", () => {
    expect(buildConfig({ config_keys: ["calendar_id"] }, { calendar_id: " primary ", other: "x" })).toEqual({ calendar_id: "primary" });
  });

  it("maps catalogue field rules to field errors without values", () => {
    const errs = credentialFieldErrors({
      detail: "Credential fields are invalid",
      fields: [
        { field: "auth_token", rule: "32 hex characters" },
        { field: "account_sid", rule: "required" },
      ],
    });
    expect(errs).toEqual({ "credentials.auth_token": "Must be 32 hex characters", "credentials.account_sid": "Required" });
    expect(credentialFieldErrors({ detail: "Unknown configuration keys", fields: ["colour"] })).toEqual({ "config.colour": "Not a valid setting for this provider" });
  });

  it("hides platform-only providers from tenants", () => {
    expect(tenantProviders([twilio, { ...twilio, provider: "google_oauth_client", platform_only: true }]).map((p) => p.provider)).toEqual(["twilio"]);
  });
});

describe("connection state", () => {
  it("offers no actions on a revoked connection", () => {
    expect(Object.values(connectionActions({ status: "revoked", auth_type: "static" })).some(Boolean)).toBe(false);
  });
  it("offers enable and revoke on a disabled connection", () => {
    expect(connectionActions({ status: "disabled", auth_type: "static" })).toEqual({ test: false, rotate: false, disable: false, enable: true, revoke: true });
  });
  it("does not offer static rotation for OAuth connections", () => {
    expect(connectionActions({ status: "active", auth_type: "oauth" }).rotate).toBe(false);
  });
  it("labels health and rotation", () => {
    expect(healthInfo("rotation_overdue").tone).toBe("warn");
    expect(healthInfo("something_new").label).toBe("something new");
    const now = Date.parse("2026-01-10T00:00:00Z");
    expect(rotationStatus(null, now).label).toBe("Not scheduled");
    expect(rotationStatus("2026-01-09T00:00:00Z", now)).toEqual({ label: "Overdue", tone: "danger" });
    expect(rotationStatus("2026-01-15T00:00:00Z", now)).toEqual({ label: "Due in 5 days", tone: "warn" });
    expect(rotationStatus("2026-03-15T00:00:00Z", now).tone).toBe("neutral");
  });
  it("uses the provider in the revoke phrase", () => {
    expect(revokePhrase({ provider: "twilio" })).toBe("revoke twilio");
  });
});

describe("OAuth", () => {
  it("follows only absolute https URLs without credentials", () => {
    expect(safeProviderUrl("https://accounts.google.com/o/oauth2/auth?state=x")).toBe("https://accounts.google.com/o/oauth2/auth?state=x");
    expect(safeProviderUrl("http://accounts.google.com/")).toBeNull();
    expect(safeProviderUrl("javascript:alert(1)")).toBeNull();
    expect(safeProviderUrl("/relative/path")).toBeNull();
    expect(safeProviderUrl("//evil.example/x")).toBeNull();
    expect(safeProviderUrl("https://user:pw@evil.example/")).toBeNull();
    expect(safeProviderUrl(42)).toBeNull();
  });

  it("parses the callback redirect defensively", () => {
    const ok = parseOAuthReturn(new URLSearchParams("oauth=active&provider=hubspot&connection=0b6f6e9e-1c1a-4c58-9d9e-6b0d7f1a2b3c"));
    expect(ok).toMatchObject({ tone: "ok", title: "HubSpot CRM connected", connectionId: "0b6f6e9e-1c1a-4c58-9d9e-6b0d7f1a2b3c" });
    const bad = parseOAuthReturn(new URLSearchParams("oauth=denied&provider=<script>&connection=../../x"));
    expect(bad).toMatchObject({ tone: "warn", provider: "the provider", connectionId: null });
    expect(parseOAuthReturn(new URLSearchParams("tab=x"))).toBeNull();
  });
});

describe("webhooks", () => {
  it("accepts https receivers only", () => {
    expect(validateUrl("https://hooks.example.com/nexus")).toBeNull();
    expect(validateUrl("http://hooks.example.com/nexus")).toMatch(/HTTPS/);
    expect(validateUrl("https://u:p@hooks.example.com/x")).toMatch(/credentials/);
    expect(validateUrl("nope")).not.toBeNull();
  });

  it("checks secret length and required fields", () => {
    expect(validateSecret("x".repeat(31))).not.toBeNull();
    expect(validateSecret("x".repeat(32))).toBeNull();
    const errs = validateEndpointForm({ url: "https://a.example.com/x", description: "", event_types: [], secret: "" }, { requireSecret: true });
    expect(Object.keys(errs).sort()).toEqual(["event_types", "secret"]);
    expect(validateEndpointForm({ url: "https://a.example.com/x", description: "", event_types: ["lead.created"], secret: "" }, { requireSecret: false })).toEqual({});
  });

  it("generates a url-safe secret of the minimum length or more", () => {
    const s = generateSecret((b) => b.fill(255));
    expect(s.length).toBeGreaterThanOrEqual(32);
    expect(s).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("patches only changed fields with the expected version", () => {
    const ep: Endpoint = {
      id: "e", url: "https://a.example.com/x", description: null, event_types: ["lead.created", "call.completed"], status: "active",
      consecutive_failures: 0, last_delivery_at: null, secret_ref: "ref", secret_version: 1, version: 7, created_at: "", updated_at: "",
    };
    expect(endpointPatch(ep, { url: ep.url, description: "", event_types: ["call.completed", "lead.created"] })).toBeNull();
    expect(endpointPatch(ep, { url: "https://b.example.com/y", description: " Ops ", event_types: ["lead.created"] })).toEqual({
      expected_version: 7, url: "https://b.example.com/y", description: "Ops", event_types: ["lead.created"],
    });
  });

  it("redelivers failed or canceled deliveries only", () => {
    expect(canRedeliver("failed")).toBe(true);
    expect(canRedeliver("succeeded")).toBe(false);
    expect(canRedeliver("queued")).toBe(false);
  });
});

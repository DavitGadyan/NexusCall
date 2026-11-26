/**
 * MCP registry helpers (services/mcp_registry). A server's identity is its endpoint, allowed hosts,
 * credential reference, service identity and trust level: changing any of them clears the reviewed pin and
 * disables the server until it is re-reviewed, so the form warns before saving.
 */

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export type McpServer = {
  id: string;
  key: string;
  display_name: string;
  description: string;
  purpose: string;
  owner: string;
  environment: string;
  transport: string;
  endpoint: string;
  allowed_hosts: string[];
  vault_ref: string | null;
  service_identity: string;
  trust_level: "internal" | "verified" | "third_party" | string;
  enabled: boolean;
  revoked: boolean;
  revoked_reason: string | null;
  disabled_reason: string | null;
  health: string;
  pinned_server_name: string | null;
  pinned_version: string | null;
  pinned_sha256: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  last_verified_at: string | null;
  max_concurrency: number;
  max_request_bytes: number;
  max_response_bytes: number;
  rate_per_minute: number;
  timeout_ms: number;
  row_version: number;
  created_at: string;
  updated_at: string;
};

export type ServerVersion = { revision: number; change: string; changed_by: string; created_at: string; snapshot: Record<string, unknown> };
export type McpServerDetail = McpServer & { versions: ServerVersion[] };

export type ToolDef = {
  tool_id: string;
  kind: string;
  server_id: string | null;
  display_name: string;
  description: string;
  risk_class: number;
  side_effecting: boolean;
  requires_approval: boolean;
  data_classification: string;
  enabled: boolean;
  revoked: boolean;
  revoked_reason: string | null;
  disabled_reason: string | null;
  rate_per_minute: number;
  timeout_ms: number;
  max_output_bytes: number;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  target: Record<string, unknown>;
  version: number;
  updated_at: string;
};

export const IDENTITY_FIELDS = ["endpoint", "allowed_hosts", "vault_ref", "service_identity", "trust_level"] as const;

export const TRUST_LEVELS = [
  { value: "internal", label: "Internal" },
  { value: "verified", label: "Verified third party" },
  { value: "third_party", label: "Third party" },
] as const;

export type ServerForm = {
  display_name: string;
  description: string;
  purpose: string;
  owner: string;
  endpoint: string;
  allowed_hosts: string;
  vault_ref: string;
  service_identity: string;
  trust_level: string;
  rate_per_minute: string;
  timeout_ms: string;
  max_concurrency: string;
};

export function serverToForm(s: McpServer): ServerForm {
  return {
    display_name: s.display_name,
    description: s.description,
    purpose: s.purpose,
    owner: s.owner,
    endpoint: s.endpoint,
    allowed_hosts: s.allowed_hosts.join("\n"),
    vault_ref: s.vault_ref ?? "",
    service_identity: s.service_identity,
    trust_level: s.trust_level,
    rate_per_minute: String(s.rate_per_minute),
    timeout_ms: String(s.timeout_ms),
    max_concurrency: String(s.max_concurrency),
  };
}

export function parseHosts(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((h) => h.trim().toLowerCase()).filter(Boolean))];
}

function int(v: string): number | null {
  return /^\d{1,7}$/.test(v.trim()) ? Number(v.trim()) : null;
}

export type ServerPatchResult = { patch: Record<string, unknown> | null; identityChanged: boolean; errors: Record<string, string> };

export function serverPatch(s: McpServer, f: ServerForm): ServerPatchResult {
  const errors: Record<string, string> = {};
  const patch: Record<string, unknown> = { expected_version: s.row_version };
  const text = (key: keyof ServerForm & keyof McpServer, min: number, max: number) => {
    const v = f[key].trim();
    if (v === String(s[key] ?? "")) return;
    if (v.length < min || v.length > max) errors[key] = `Between ${min} and ${max} characters.`;
    else patch[key] = v;
  };
  text("display_name", 2, 200);
  text("description", 0, 2000);
  text("purpose", 5, 2000);
  text("owner", 2, 200);
  const endpoint = f.endpoint.trim();
  if (endpoint !== s.endpoint) {
    if (!/^https:\/\//i.test(endpoint) || endpoint.length > 2048) errors.endpoint = "An https:// URL without a query string.";
    else if (/[?#]/.test(endpoint)) errors.endpoint = "No query string or fragment.";
    else patch.endpoint = endpoint;
  }
  const hosts = parseHosts(f.allowed_hosts);
  if (hosts.join(",") !== s.allowed_hosts.join(",")) {
    if (hosts.length === 0) errors.allowed_hosts = "At least one host.";
    else patch.allowed_hosts = hosts;
  }
  const vault = f.vault_ref.trim();
  if (vault !== (s.vault_ref ?? "")) {
    if (vault === "") patch.clear_vault_ref = true;
    else if (vault.length > 255) errors.vault_ref = "At most 255 characters.";
    else patch.vault_ref = vault;
  }
  const identity = f.service_identity.trim();
  if (identity !== s.service_identity) {
    if (!/^[a-z][a-z0-9-]{2,63}$/.test(identity)) errors.service_identity = "Lowercase letters, digits and dashes (3–64).";
    else patch.service_identity = identity;
  }
  if (f.trust_level !== s.trust_level) patch.trust_level = f.trust_level;
  const bounded = (key: "rate_per_minute" | "timeout_ms" | "max_concurrency", min: number, max: number) => {
    const n = int(f[key]);
    if (n === s[key]) return;
    if (n === null || n < min || n > max) errors[key] = `Between ${min} and ${max}.`;
    else patch[key] = n;
  };
  bounded("rate_per_minute", 1, 6000);
  bounded("timeout_ms", 100, 30000);
  bounded("max_concurrency", 1, 64);
  const identityChanged = IDENTITY_FIELDS.some((k) => k in patch) || "clear_vault_ref" in patch;
  if (Object.keys(errors).length) return { patch: null, identityChanged, errors };
  return { patch: Object.keys(patch).length > 1 ? patch : null, identityChanged, errors };
}

export type PinForm = { server_name: string; version: string; sha256: string };

export function validatePin(p: PinForm): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!p.server_name.trim() || p.server_name.trim().length > 200) errors.server_name = "The name the server reports (1–200 characters).";
  if (!/^[A-Za-z0-9.+_-]{1,64}$/.test(p.version.trim())) errors.version = "The reviewed version, e.g. 1.4.2.";
  if (!/^[0-9a-f]{64}$/.test(p.sha256.trim())) errors.sha256 = "64 lowercase hex characters.";
  return errors;
}

export function isPinned(s: Pick<McpServer, "pinned_version" | "pinned_sha256" | "reviewed_by">): boolean {
  return Boolean(s.pinned_version && s.pinned_sha256 && s.reviewed_by);
}

export function serverState(s: McpServer): { label: string; tone: Tone } {
  if (s.revoked) return { label: "Revoked", tone: "danger" };
  if (s.health === "identity_mismatch") return { label: "Identity mismatch", tone: "danger" };
  if (!isPinned(s)) return { label: "Needs review", tone: "warn" };
  if (!s.enabled) return { label: "Disabled", tone: "neutral" };
  if (s.health === "down") return { label: "Down", tone: "danger" };
  if (s.health === "degraded") return { label: "Degraded", tone: "warn" };
  if (s.health === "healthy") return { label: "Healthy", tone: "ok" };
  return { label: "Enabled", tone: "brand" };
}

/** Why "Enable" is not available, mirroring the service's checks. */
export function enableBlocker(s: McpServer): string | null {
  if (s.revoked) return "The server is revoked.";
  if (!isPinned(s)) return "Pin a reviewed version (version and sha256) first.";
  if (s.health === "identity_mismatch") return "The server reported an unexpected identity; review and re-pin it.";
  return null;
}

export const RISK_CLASSES: Record<number, { label: string; help: string; tone: Tone }> = {
  0: { label: "0 · Read-only", help: "Low-impact reads: approved knowledge, non-sensitive metadata.", tone: "neutral" },
  1: { label: "1 · Sensitive read", help: "Reads personal data, recordings, transcripts or mailbox content.", tone: "neutral" },
  2: { label: "2 · Reversible write", help: "Updates a field, creates a draft or a tentative booking.", tone: "brand" },
  3: { label: "3 · External side effect", help: "Sends, calls or confirms; always needs human approval.", tone: "warn" },
  4: { label: "4 · Privileged", help: "Security or infrastructure operation; never grantable to an agent.", tone: "danger" },
};

export function riskInfo(n: number): { label: string; help: string; tone: Tone } {
  return RISK_CLASSES[n] ?? { label: String(n), help: "", tone: "neutral" };
}

export function toolState(t: ToolDef): { label: string; tone: Tone } {
  if (t.revoked) return { label: "Revoked", tone: "danger" };
  if (!t.enabled) return { label: "Disabled", tone: "neutral" };
  return { label: "Enabled", tone: "ok" };
}

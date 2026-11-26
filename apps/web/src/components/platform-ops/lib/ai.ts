/**
 * AI gateway control-plane helpers: prices, kill switches and tool-policy proposals. Validation mirrors
 * services/ai_gateway/ai_gateway/api/admin.py and services/mcp_registry/mcp_registry/policy.py so the
 * form says what is wrong before the request; the services validate again and are authoritative.
 */

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"] as const;
export type Classification = (typeof CLASSIFICATIONS)[number];

export type AiModel = {
  model_id: string;
  provider: string;
  display_name: string;
  enabled: boolean;
  input_price_micros_per_mtok: number;
  output_price_micros_per_mtok: number;
  max_output_tokens: number;
  allowed_classifications: string[];
  purposes: string[];
  updated_at: string;
  updated_by: string | null;
};

export type Route = { purpose: string; models: string[]; failover_allowed: boolean; updated_at: string; updated_by: string | null };
export type KillSwitch = { scope: string; target: string; engaged: boolean; reason: string; changed_by: string | null; updated_at: string };

export type SecurityEvent = {
  id: string;
  tenant_id: string;
  request_id: string | null;
  kind: string;
  severity: "info" | "low" | "medium" | "high" | string;
  owasp: string;
  agent_id: string | null;
  tool_id: string | null;
  detail: string;
  created_at: string;
};

/** "3.5" → 3_500_000. Up to 6 decimals; null when not a non-negative amount. */
export function usdToMicros(input: string): number | null {
  const v = input.trim();
  if (!/^\d{1,9}(\.\d{1,6})?$/.test(v)) return null;
  const [whole = "0", frac = ""] = v.split(".");
  return Number(whole) * 1_000_000 + Number(frac.padEnd(6, "0"));
}

export function microsToUsdInput(micros: number): string {
  const whole = Math.floor(micros / 1_000_000);
  const frac = String(micros % 1_000_000).padStart(6, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : String(whole);
}

export type ModelForm = { enabled: boolean; input: string; output: string; classifications: string[] };

export function modelPatch(m: AiModel, f: ModelForm): { patch: Record<string, unknown> | null; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const patch: Record<string, unknown> = {};
  const input = usdToMicros(f.input);
  const output = usdToMicros(f.output);
  if (input === null) errors.input_price_micros_per_mtok = "Enter a price in USD, e.g. 3 or 0.25.";
  if (output === null) errors.output_price_micros_per_mtok = "Enter a price in USD, e.g. 15.";
  if (f.classifications.length === 0) errors.allowed_classifications = "Allow at least one classification.";
  if (Object.keys(errors).length) return { patch: null, errors };
  if (f.enabled !== m.enabled) patch.enabled = f.enabled;
  if (input !== m.input_price_micros_per_mtok) patch.input_price_micros_per_mtok = input;
  if (output !== m.output_price_micros_per_mtok) patch.output_price_micros_per_mtok = output;
  const ordered = CLASSIFICATIONS.filter((c) => f.classifications.includes(c));
  const before = CLASSIFICATIONS.filter((c) => m.allowed_classifications.includes(c));
  if (ordered.join() !== before.join()) patch.allowed_classifications = ordered;
  return { patch: Object.keys(patch).length ? patch : null, errors };
}

export function validateRoute(models: string[], registered: ReadonlySet<string>): string | null {
  if (models.length === 0) return "Choose at least one model.";
  if (models.length > 5) return "At most five models per route.";
  if (new Set(models).size !== models.length) return "Each model may appear once.";
  const unknown = models.find((m) => !registered.has(m));
  if (unknown) return `${unknown} is not a registered model.`;
  return null;
}

export function moveItem<T>(list: T[], index: number, delta: -1 | 1): T[] {
  const j = index + delta;
  if (index < 0 || index >= list.length || j < 0 || j >= list.length) return list;
  const next = [...list];
  const a = next[index] as T;
  next[index] = next[j] as T;
  next[j] = a;
  return next;
}

export const KILL_SCOPES = [
  { value: "platform", label: "Whole platform", hint: "Stops every AI request on the platform. Target is *." },
  { value: "provider", label: "Provider", hint: "e.g. anthropic" },
  { value: "model", label: "Model", hint: "A registered model id." },
  { value: "tenant", label: "Tenant", hint: "Tenant UUID." },
  { value: "agent", label: "Agent", hint: "Agent UUID." },
] as const;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TARGET = /^(\*|[a-z0-9][a-z0-9.-]{1,79})$/;

export function validateKillSwitch(v: { scope: string; target: string; reason: string }): Record<string, string> {
  const errors: Record<string, string> = {};
  const target = v.target.trim();
  if (!KILL_SCOPES.some((s) => s.value === v.scope)) errors.scope = "Choose a scope.";
  if (v.scope === "platform" && target !== "*") errors.target = "The platform switch targets *.";
  else if (v.scope !== "platform" && target === "*") errors.target = "Only the platform switch targets *.";
  else if ((v.scope === "tenant" || v.scope === "agent") && !UUID.test(target)) errors.target = "Enter a UUID.";
  else if (!TARGET.test(target)) errors.target = "Lowercase letters, digits, dots and dashes (2–80).";
  const reason = v.reason.trim();
  if (reason.length < 5) errors.reason = "At least 5 characters.";
  if (reason.length > 300) errors.reason = "At most 300 characters.";
  return errors;
}

/** The typed confirmation for a kill switch names the switch itself. */
export function killPhrase(scope: string, target: string, engage: boolean): string {
  return `${engage ? "engage" : "release"} ${scope}:${target.trim()}`;
}

export const SEVERITIES = ["info", "low", "medium", "high"] as const;

export function severityTone(s: string): Tone {
  if (s === "high") return "danger";
  if (s === "medium") return "warn";
  if (s === "low") return "brand";
  return "neutral";
}

// ------------------------------------------------------------------ tool policy (mcp_registry/policy.py)
export type ToolPolicy = {
  max_agent_risk_class: number;
  approval_required_classes: number[];
  disabled_tools: string[];
  agent_calls_per_minute: number;
  mcp_tools_enabled: boolean;
  mcp_require_pinned: true;
};

export type PolicyVersion = {
  version: number;
  status: string;
  policy: Record<string, unknown>;
  proposal_id: string | null;
  rollback_of: number | null;
  deployed_by: string;
  deployed_at: string;
};

export type Policies = { active_version: number; active: Record<string, unknown>; items: PolicyVersion[] };

export type Proposal = {
  id: string;
  base_version: number;
  policy: Record<string, unknown>;
  reason: string;
  status: "pending" | "approved" | "rejected" | "deployed" | "expired" | string;
  proposed_by: string;
  approved_by: string | null;
  rejected_by: string | null;
  deployed_by: string | null;
  decision_note: string | null;
  decided_at: string | null;
  deployed_at: string | null;
  deployed_version: number | null;
  expires_at: string;
  created_at: string;
};

const TOOL_ID = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

export function toPolicy(raw: Record<string, unknown>): ToolPolicy {
  const num = (v: unknown, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
  return {
    max_agent_risk_class: num(raw.max_agent_risk_class, 3),
    approval_required_classes: Array.isArray(raw.approval_required_classes) ? raw.approval_required_classes.filter((x): x is number => typeof x === "number") : [3],
    disabled_tools: Array.isArray(raw.disabled_tools) ? raw.disabled_tools.filter((x): x is string => typeof x === "string") : [],
    agent_calls_per_minute: num(raw.agent_calls_per_minute, 60),
    mcp_tools_enabled: raw.mcp_tools_enabled !== false,
    mcp_require_pinned: true,
  };
}

export function parseToolList(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean))].sort();
}

export function validatePolicy(p: ToolPolicy): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!Number.isInteger(p.max_agent_risk_class) || p.max_agent_risk_class < 0 || p.max_agent_risk_class > 3)
    errors.max_agent_risk_class = "Between 0 and 3. Class 4 tools can never be granted to agents.";
  if (p.approval_required_classes.some((c) => !Number.isInteger(c) || c < 0 || c > 3)) errors.approval_required_classes = "Classes must be within 0–3.";
  else if (!p.approval_required_classes.includes(3)) errors.approval_required_classes = "Class 3 tools always require approval.";
  if (p.disabled_tools.length > 200) errors.disabled_tools = "At most 200 tools.";
  else {
    const bad = p.disabled_tools.find((t) => !TOOL_ID.test(t) || t.length > 80);
    if (bad) errors.disabled_tools = `Not a tool id: ${bad.slice(0, 80)}`;
  }
  if (!Number.isInteger(p.agent_calls_per_minute) || p.agent_calls_per_minute < 1 || p.agent_calls_per_minute > 6000)
    errors.agent_calls_per_minute = "Between 1 and 6000.";
  return errors;
}

export function normalizePolicy(p: ToolPolicy): ToolPolicy {
  return { ...p, approval_required_classes: [...new Set(p.approval_required_classes)].sort(), disabled_tools: [...new Set(p.disabled_tools)].sort() };
}

const POLICY_LABELS: Record<keyof ToolPolicy, string> = {
  max_agent_risk_class: "Highest risk class agents may use",
  approval_required_classes: "Classes that need human approval",
  disabled_tools: "Disabled tools",
  agent_calls_per_minute: "Tool calls per agent per minute",
  mcp_tools_enabled: "MCP tools enabled",
  mcp_require_pinned: "MCP servers must be pinned",
};

export function policyLabel(key: string): string {
  return POLICY_LABELS[key as keyof ToolPolicy] ?? key.replace(/_/g, " ");
}

export function formatPolicyValue(v: unknown): string {
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (v === null || v === undefined) return "—";
  return String(v);
}

export function policyDiff(before: Record<string, unknown>, after: Record<string, unknown>): { key: string; before: string; after: string }[] {
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
  return keys
    .map((key) => ({ key, before: formatPolicyValue(before[key]), after: formatPolicyValue(after[key]) }))
    .filter((d) => d.before !== d.after);
}

export type ProposalActions = {
  approve: boolean;
  reject: boolean;
  deploy: boolean;
  /** why approve is not offered to this user, shown next to the proposal */
  approveNote: string | null;
  deployNote: string | null;
};

/**
 * Dual control: the proposer can never approve their own proposal (the service answers 403). Deploying
 * needs an approved proposal whose base is still the active version.
 */
export function proposalActions(p: Proposal, me: string, activeVersion: number, now = Date.now()): ProposalActions {
  const expired = p.status === "pending" && new Date(p.expires_at).getTime() <= now;
  const own = p.proposed_by === me;
  const stale = p.base_version !== activeVersion;
  return {
    approve: p.status === "pending" && !expired && !own,
    reject: (p.status === "pending" && !expired) || p.status === "approved",
    deploy: p.status === "approved" && !stale,
    approveNote:
      p.status !== "pending" ? null : expired ? "Expired: propose again." : own ? "You proposed this change; a different administrator must approve it." : null,
    deployNote: p.status === "approved" && stale ? `The active policy changed (v${activeVersion}) since this was proposed on v${p.base_version}; propose again.` : null,
  };
}

const PROPOSAL_TONE: Record<string, Tone> = { pending: "warn", approved: "brand", deployed: "ok", rejected: "neutral", expired: "neutral" };

export function proposalTone(status: string): Tone {
  return PROPOSAL_TONE[status] ?? "neutral";
}

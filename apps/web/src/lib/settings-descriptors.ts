/**
 * Form descriptors for every settings section (mirrors services/tenant_service/tenant_service/settings_schema.py).
 * The server validates and bounds every value; locked fields are platform policy and are shown read-only.
 */
export type Option = { value: string; label: string };

export type FieldDescriptor = {
  key: string;
  label: string;
  hint?: string;
  /** reason shown when the control is fixed by platform policy */
  locked?: string;
} & (
  | { kind: "text"; maxLength?: number; placeholder?: string }
  | { kind: "url"; placeholder?: string }
  | { kind: "color" }
  | { kind: "number"; min?: number; max?: number; suffix?: string }
  | { kind: "boolean" }
  | { kind: "select"; options: Option[] }
  | { kind: "list"; placeholder?: string; upper?: boolean }
  | { kind: "multi"; options: Option[] }
  | { kind: "window" }
  | { kind: "timezone" }
);

export type SectionDescriptor = {
  id: string;
  title: string;
  description: string;
  fields: FieldDescriptor[];
  /** the API requires step-up MFA to save */
  highRisk?: boolean;
  /** typed confirmation before saving */
  confirm?: { phrase: string; message: string };
};

export const MODULE_OPTIONS: Option[] = [
  { value: "crm", label: "CRM" },
  { value: "calls", label: "Calls" },
  { value: "email_campaigns", label: "Email campaigns" },
  { value: "inbox", label: "Inbox" },
  { value: "bookings", label: "Bookings" },
  { value: "analytics", label: "Analytics" },
  { value: "integrations", label: "Integrations" },
  { value: "ai_agents", label: "AI agents" },
];

export const DAYS: Option[] = [
  { value: "mon", label: "Mon" },
  { value: "tue", label: "Tue" },
  { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" },
  { value: "fri", label: "Fri" },
  { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];

const PLATFORM_MAX = "Platform limits apply; values above them are rejected.";

export const TENANT_SETTINGS: SectionDescriptor[] = [
  {
    id: "profile",
    title: "Profile",
    description: "Workspace name, timezone, locale and business hours.",
    fields: [
      { key: "name", label: "Workspace name", kind: "text", maxLength: 200 },
      { key: "timezone", label: "Timezone", kind: "timezone" },
      { key: "locale", label: "Locale", kind: "text", maxLength: 5, placeholder: "en-US", hint: "Language and region, e.g. en-US." },
      { key: "business_hours", label: "Business hours", kind: "window" },
    ],
  },
  {
    id: "branding",
    title: "Branding",
    description: "Logo and primary colour used in this workspace.",
    fields: [
      { key: "primary_color", label: "Primary colour", kind: "color" },
      { key: "logo_url", label: "Logo URL", kind: "url", placeholder: "https://…", hint: "HTTPS only." },
    ],
  },
  {
    id: "communication",
    title: "Communication policy",
    description: "Calling and email windows, concurrency, quotas and consent safeguards.",
    fields: [
      { key: "calls_enabled", label: "Calls enabled", kind: "boolean", hint: "Workspace kill switch. The platform kill switch also applies." },
      { key: "emails_enabled", label: "Emails enabled", kind: "boolean", hint: "Workspace kill switch. The platform kill switch also applies." },
      { key: "calling_window", label: "Calling window", kind: "window", hint: "In each contact's local timezone." },
      { key: "email_window", label: "Email sending window", kind: "window" },
      { key: "max_concurrent_calls", label: "Max concurrent calls", kind: "number", min: 1, hint: PLATFORM_MAX },
      { key: "max_daily_calls", label: "Max calls per day", kind: "number", min: 1, hint: PLATFORM_MAX },
      { key: "email_daily_quota", label: "Email daily quota", kind: "number", min: 1, hint: PLATFORM_MAX },
      { key: "allowed_call_countries", label: "Allowed call countries", kind: "list", upper: true, placeholder: "US, CA", hint: "ISO 3166-1 alpha-2 codes, comma separated." },
      { key: "ai_disclosure_enabled", label: "AI disclosure at call start", kind: "boolean" },
      { key: "recording_enabled", label: "Record calls", kind: "boolean" },
      { key: "recording_consent_prompt", label: "Ask for recording consent", kind: "boolean" },
      { key: "honor_opt_out_across_channels", label: "Honour opt-outs across all channels", kind: "boolean", locked: "Opt-outs always apply to every channel (platform policy)." },
    ],
  },
  {
    id: "retention",
    title: "Retention",
    description: "How long content is kept before deletion.",
    fields: [
      { key: "recordings_days", label: "Recordings", kind: "number", min: 1, suffix: "days", hint: PLATFORM_MAX },
      { key: "transcripts_days", label: "Transcripts", kind: "number", min: 1, suffix: "days", hint: PLATFORM_MAX },
      { key: "email_content_days", label: "Email content", kind: "number", min: 1, suffix: "days", hint: PLATFORM_MAX },
      { key: "attachments_days", label: "Attachments", kind: "number", min: 1, suffix: "days", hint: PLATFORM_MAX },
    ],
  },
  {
    id: "ai",
    title: "AI",
    description: "Models, budget and data handling for AI features.",
    fields: [
      { key: "allowed_models", label: "Allowed models", kind: "list", placeholder: "claude-haiku-5-5, claude-sonnet-5-5", hint: "Only models the platform allows can be used." },
      { key: "monthly_budget_usd", label: "Monthly budget", kind: "number", min: 0, suffix: "USD", hint: PLATFORM_MAX },
      {
        key: "data_classification_max",
        label: "Highest data classification sent to models",
        kind: "select",
        options: [
          { value: "internal", label: "Internal" },
          { value: "confidential", label: "Confidential" },
          { value: "restricted", label: "Restricted" },
        ],
      },
      { key: "draft_auto_send", label: "Send AI drafts automatically", kind: "boolean", locked: "AI-written email always needs human approval in this release." },
      { key: "store_prompt_content", label: "Store prompt content", kind: "boolean", locked: "Prompt content is not stored (platform policy)." },
    ],
  },
  {
    id: "approval",
    title: "Approval rules",
    description: "Human review of AI-written replies.",
    fields: [
      { key: "require_approval_for_ai_email", label: "Require approval for AI email", kind: "boolean", locked: "Always on: AI replies are never sent without approval." },
      { key: "approver_permission", label: "Approver permission", kind: "text", locked: "Approvers need emails:drafts:approve." },
      { key: "escalate_sensitive_intents", label: "Escalate sensitive intents", kind: "boolean", hint: "Complaints, legal and cancellation requests go to a senior reviewer." },
    ],
  },
  {
    id: "modules",
    title: "Modules",
    description: "Modules enabled for this workspace, within what the platform enables.",
    fields: [{ key: "enabled", label: "Enabled modules", kind: "multi", options: MODULE_OPTIONS }],
  },
];

export const PLATFORM_SETTINGS: SectionDescriptor[] = [
  {
    id: "branding",
    title: "Branding & UI",
    description: "Platform name, colours and defaults.",
    fields: [
      { key: "platform_name", label: "Platform name", kind: "text", maxLength: 80 },
      { key: "primary_color", label: "Primary colour", kind: "color" },
      { key: "logo_url", label: "Logo URL", kind: "url", placeholder: "https://…" },
      {
        key: "default_theme",
        label: "Default theme",
        kind: "select",
        options: [
          { value: "system", label: "System" },
          { value: "light", label: "Light" },
          { value: "dark", label: "Dark" },
        ],
      },
      {
        key: "default_landing_page",
        label: "Default landing page",
        kind: "select",
        options: ["/app", "/app/calls", "/app/inbox", "/app/analytics"].map((v) => ({ value: v, label: v })),
      },
    ],
  },
  {
    id: "security",
    title: "Security policies",
    description: "Session lifetimes, step-up window, invitation and support-access limits.",
    highRisk: true,
    fields: [
      { key: "session_idle_minutes", label: "Session idle timeout", kind: "number", min: 5, max: 240, suffix: "minutes" },
      { key: "session_max_hours", label: "Session maximum lifetime", kind: "number", min: 1, max: 24, suffix: "hours" },
      { key: "step_up_max_age_seconds", label: "Step-up validity", kind: "number", min: 60, max: 900, suffix: "seconds" },
      { key: "mfa_required_for_admins", label: "MFA required for administrators", kind: "boolean", locked: "MFA for administrators cannot be turned off." },
      { key: "invitation_ttl_hours", label: "Invitation lifetime", kind: "number", min: 1, max: 336, suffix: "hours" },
      { key: "support_grant_max_hours", label: "Support access maximum", kind: "number", min: 1, max: 24, suffix: "hours" },
    ],
  },
  {
    id: "limits",
    title: "Quotas & rate limits",
    description: "Upper bounds every tenant setting must respect.",
    highRisk: true,
    fields: [
      { key: "max_concurrent_calls_per_tenant", label: "Max concurrent calls per tenant", kind: "number", min: 1, max: 1000 },
      { key: "max_daily_calls_per_tenant", label: "Max daily calls per tenant", kind: "number", min: 1, max: 1_000_000 },
      { key: "max_email_daily_quota", label: "Max email daily quota", kind: "number", min: 1, max: 10_000_000 },
      { key: "api_rate_limit_per_minute", label: "API rate limit", kind: "number", min: 10, max: 100_000, suffix: "per minute" },
      { key: "campaign_launches_per_hour", label: "Campaign launches", kind: "number", min: 1, max: 1000, suffix: "per hour" },
      { key: "max_ai_monthly_budget_usd", label: "Max AI monthly budget", kind: "number", min: 0, max: 1_000_000, suffix: "USD" },
    ],
  },
  {
    id: "retention",
    title: "Retention defaults",
    description: "Maximums tenants may choose, and defaults.",
    fields: [
      { key: "max_recordings_days", label: "Max recordings", kind: "number", min: 1, max: 3650, suffix: "days" },
      { key: "default_recordings_days", label: "Default recordings", kind: "number", min: 1, max: 3650, suffix: "days" },
      { key: "max_transcripts_days", label: "Max transcripts", kind: "number", min: 1, max: 3650, suffix: "days" },
      { key: "max_email_content_days", label: "Max email content", kind: "number", min: 1, max: 3650, suffix: "days" },
      { key: "max_attachments_days", label: "Max attachments", kind: "number", min: 1, max: 3650, suffix: "days" },
      { key: "audit_retention_days", label: "Audit retention", kind: "number", min: 365, max: 3650, suffix: "days" },
    ],
  },
  {
    id: "maintenance",
    title: "Maintenance mode",
    description: "Read-only or maintenance mode for the whole platform.",
    highRisk: true,
    confirm: { phrase: "maintenance", message: "Changing the maintenance mode affects every tenant immediately." },
    fields: [
      {
        key: "mode",
        label: "Mode",
        kind: "select",
        options: [
          { value: "off", label: "Off" },
          { value: "read_only", label: "Read-only" },
          { value: "maintenance", label: "Maintenance" },
        ],
      },
      { key: "message", label: "Message shown to users", kind: "text", maxLength: 300 },
    ],
  },
  {
    id: "modules",
    title: "Enabled modules",
    description: "Modules available to tenants.",
    fields: [{ key: "enabled", label: "Enabled modules", kind: "multi", options: MODULE_OPTIONS }],
  },
  {
    id: "kill_switches",
    title: "Kill switches",
    description: "Stop all calls, emails or AI across every tenant. Use in incidents.",
    highRisk: true,
    confirm: { phrase: "kill switches", message: "Kill switches take effect for every tenant within seconds." },
    fields: [
      { key: "calls_enabled", label: "Calls enabled (all tenants)", kind: "boolean" },
      { key: "emails_enabled", label: "Emails enabled (all tenants)", kind: "boolean" },
      { key: "ai_enabled", label: "AI enabled (all tenants)", kind: "boolean" },
      { key: "reason", label: "Reason", kind: "text", maxLength: 300, hint: "Shown to operators and recorded in the audit log." },
    ],
  },
];

/** Values sent back on save: everything loaded (unknown keys included) except `_meta` keys. */
export function sectionPayload(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([k]) => !k.startsWith("_")));
}

/** Field errors keyed by their top-level field (nested paths such as `calling_window.start` roll up). */
export function errorsByField(fields: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [path, msg] of Object.entries(fields)) {
    const top = path.split(".")[0] ?? path;
    out[top] = out[top] ? `${out[top]}; ${msg}` : msg;
  }
  return out;
}

export function parseList(raw: string, upper = false): string[] {
  return raw
    .split(/[,\s]+/)
    .map((s) => (upper ? s.trim().toUpperCase() : s.trim()))
    .filter(Boolean);
}

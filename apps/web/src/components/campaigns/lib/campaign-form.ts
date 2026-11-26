/**
 * Call campaign builder model: validation that mirrors campaign_service `CampaignIn` and the tenant-limit
 * checks of `validation.limit_problems`, problem-code labels and the state machine's action list.
 * The service re-validates everything at schedule and launch time.
 */
import { z } from "zod";

export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Day = (typeof DAYS)[number];
export const DAY_LABEL: Record<Day, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

export type Campaign = {
  id: string;
  name: string;
  description: string | null;
  state: string;
  agent_id: string | null;
  segment_id: string | null;
  timezone: string;
  window_start: string;
  window_end: string;
  window_days: string[];
  calls_per_minute: number;
  max_concurrency: number;
  max_attempts: number;
  retry_delay_seconds: number;
  start_at: string | null;
  end_at: string | null;
  launched_at: string | null;
  completed_at: string | null;
  failure_reason: string | null;
  snapshot_status: string;
  snapshot_total: number;
  allowed_actions?: string[];
  version: number;
  created_at: string;
  updated_at: string;
};

export type CampaignReport = {
  campaign_id: string;
  state: string;
  snapshot_status: string;
  audience: number;
  attempts: number;
  calls_dispatched: number;
  contacts_by_state: Record<string, number>;
  outcomes: Record<string, number>;
};

export type CampaignContact = {
  id: string;
  contact_id: string;
  state: string;
  attempts: number;
  outcome: string | null;
  skip_reason: string | null;
  last_call_id: string | null;
  next_attempt_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KillSwitch = { scope: string; channel: string; engaged: boolean; reason: string | null; changed_by: string | null; updated_at: string | null };

/** Tenant `communication` settings section (read only when the user may read settings). */
export type TenantLimits = {
  max_concurrent_calls?: number;
  max_daily_calls?: number;
  calls_enabled?: boolean;
  calling_window?: { start?: string; end?: string; days?: string[] } | null;
};

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Text-valued form state (inputs), converted by `buildCampaign`. */
export type CampaignForm = {
  name: string;
  description: string;
  agent_id: string;
  segment_id: string;
  timezone: string;
  window_start: string;
  window_end: string;
  window_days: Day[];
  calls_per_minute: string;
  max_concurrency: string;
  max_attempts: string;
  retry_delay_minutes: string;
  start_at: string; // datetime-local, in the browser's zone
  end_at: string;
};

export function defaultForm(timezone = "UTC"): CampaignForm {
  return {
    name: "",
    description: "",
    agent_id: "",
    segment_id: "",
    timezone,
    window_start: "09:00",
    window_end: "17:00",
    window_days: ["mon", "tue", "wed", "thu", "fri"],
    calls_per_minute: "10",
    max_concurrency: "5",
    max_attempts: "3",
    retry_delay_minutes: "60",
    start_at: "",
    end_at: "",
  };
}

/** ISO (UTC) → value for `<input type="datetime-local">` in the browser's zone. */
export function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function campaignToForm(c: Campaign): CampaignForm {
  return {
    name: c.name,
    description: c.description ?? "",
    agent_id: c.agent_id ?? "",
    segment_id: c.segment_id ?? "",
    timezone: c.timezone,
    window_start: c.window_start,
    window_end: c.window_end,
    window_days: DAYS.filter((d) => c.window_days.includes(d)),
    calls_per_minute: String(c.calls_per_minute),
    max_concurrency: String(c.max_concurrency),
    max_attempts: String(c.max_attempts),
    retry_delay_minutes: String(Math.round(c.retry_delay_seconds / 60)),
    start_at: toLocalInput(c.start_at),
    end_at: toLocalInput(c.end_at),
  };
}

const int = (min: number, max: number, label: string) =>
  z
    .string()
    .trim()
    .regex(/^\d+$/, `${label} must be a whole number`)
    .transform(Number)
    .refine((n) => n >= min && n <= max, `${label} must be between ${min} and ${max}`);

const localDate = z
  .string()
  .trim()
  .refine((v) => v === "" || !Number.isNaN(new Date(v).getTime()), "Enter a valid date and time")
  .transform((v) => (v ? new Date(v).toISOString() : null));

export const campaignSchema = z
  .object({
    name: z.string().trim().min(1, "Name is required").max(200, "At most 200 characters"),
    description: z.string().trim().max(1000, "At most 1000 characters").transform((v) => v || null),
    agent_id: z.string().transform((v) => v || null),
    segment_id: z.string().transform((v) => v || null),
    timezone: z.string().min(1, "Choose a timezone"),
    window_start: z.string().regex(HHMM, "Use HH:MM"),
    window_end: z.string().regex(HHMM, "Use HH:MM"),
    window_days: z.array(z.enum(DAYS)).min(1, "Pick at least one day"),
    calls_per_minute: int(1, 600, "Calls per minute"),
    max_concurrency: int(1, 1000, "Concurrent calls"),
    max_attempts: int(1, 10, "Attempts"),
    retry_delay_minutes: int(1, 10080, "Retry delay"),
    start_at: localDate,
    end_at: localDate,
  })
  .superRefine((v, ctx) => {
    if (v.window_start >= v.window_end) ctx.addIssue({ code: "custom", path: ["window_end"], message: "The window must end after it starts" });
    if (v.start_at && v.end_at && v.start_at >= v.end_at) ctx.addIssue({ code: "custom", path: ["end_at"], message: "End must be after start" });
  });

export type CampaignBody = {
  name: string;
  description: string | null;
  agent_id: string | null;
  segment_id: string | null;
  timezone: string;
  window_start: string;
  window_end: string;
  window_days: Day[];
  calls_per_minute: number;
  max_concurrency: number;
  max_attempts: number;
  retry_delay_seconds: number;
  start_at: string | null;
  end_at: string | null;
};

export function buildCampaign(form: CampaignForm): { body?: CampaignBody; errors: Record<string, string> } {
  const r = campaignSchema.safeParse(form);
  if (!r.success) {
    const errors: Record<string, string> = {};
    for (const issue of r.error.issues) {
      const key = String(issue.path[0] ?? "_form");
      if (!errors[key]) errors[key] = issue.message;
    }
    return { errors };
  }
  const { retry_delay_minutes, ...rest } = r.data;
  return { body: { ...rest, window_days: DAYS.filter((d) => rest.window_days.includes(d)), retry_delay_seconds: retry_delay_minutes * 60 }, errors: {} };
}

/** Fields the service accepts while a campaign is paused (campaign_service EDITABLE_WHEN_PAUSED). */
export const EDITABLE_WHEN_PAUSED = new Set(["window_start", "window_end", "window_days", "max_concurrency", "calls_per_minute", "end_at", "description"]);
const CLEARABLE = new Set(["description", "start_at", "end_at"]);

export function editableFields(state: string): Set<string> | "all" | "none" {
  if (state === "draft" || state === "scheduled") return "all";
  if (state === "paused") return EDITABLE_WHEN_PAUSED;
  return "none";
}

/** PATCH body with changed fields only. Non-clearable nulls are dropped (the service rejects clearing them). */
export function campaignPatch(original: Campaign, body: CampaignBody): Record<string, unknown> {
  const editable = editableFields(original.state);
  const before: Record<string, unknown> = {
    ...original,
    window_days: DAYS.filter((d) => original.window_days.includes(d)),
  };
  const patch: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(body)) {
    if (editable === "none" || (editable !== "all" && !editable.has(k))) continue;
    const a = JSON.stringify(before[k] ?? null);
    const b = JSON.stringify(v ?? null);
    if (a === b) continue;
    if (v === null && !CLEARABLE.has(k)) continue;
    if ((k === "start_at" || k === "end_at") && v && before[k] && new Date(String(before[k])).getTime() === new Date(String(v)).getTime()) continue;
    patch[k] = v;
  }
  return Object.keys(patch).length ? { ...patch, expected_version: original.version } : {};
}

/** Mirrors `limit_problems`; empty when the limits are unknown. */
export function limitProblems(body: Pick<CampaignBody, "max_concurrency" | "calls_per_minute" | "window_start" | "window_end" | "window_days">, limits: TenantLimits | null | undefined): string[] {
  if (!limits) return [];
  const problems: string[] = [];
  if (limits.calls_enabled === false) problems.push("calls_disabled_for_tenant");
  if (typeof limits.max_concurrent_calls === "number" && body.max_concurrency > limits.max_concurrent_calls) problems.push("max_concurrency_exceeds_tenant_limit");
  if (typeof limits.max_daily_calls === "number" && body.calls_per_minute > limits.max_daily_calls) problems.push("calls_per_minute_exceeds_tenant_daily_limit");
  const w = limits.calling_window;
  if (w) {
    if (body.window_start < (w.start ?? "00:00") || body.window_end > (w.end ?? "23:59")) problems.push("window_outside_tenant_calling_window");
    const allowed = new Set(w.days ?? DAYS);
    if (!body.window_days.every((d) => allowed.has(d))) problems.push("days_outside_tenant_calling_window");
  }
  return problems;
}

export const PROBLEM_LABEL: Record<string, string> = {
  agent_required: "Choose an AI agent.",
  segment_required: "Choose a segment.",
  invalid_window: "The calling window must end after it starts.",
  agent_not_voice: "The agent is not a voice agent.",
  agent_not_active: "The agent has no active version. Activate a version first.",
  agent_not_found: "The agent no longer exists.",
  segment_not_found: "The segment no longer exists.",
  calls_disabled_for_tenant: "Calls are disabled for this workspace (communication policy or platform kill switch).",
  max_concurrency_exceeds_tenant_limit: "Concurrent calls exceed the workspace limit.",
  calls_per_minute_exceeds_tenant_daily_limit: "Calls per minute exceed the workspace's daily call limit.",
  window_outside_tenant_calling_window: "The calling window is outside the workspace calling window.",
  days_outside_tenant_calling_window: "Some days are outside the workspace calling days.",
};

export function problemLabel(code: string): string {
  return PROBLEM_LABEL[code] ?? code.replace(/_/g, " ");
}

/** `problems` list carried by 422 answers of schedule / launch. */
export function problemsOf(problem: { [k: string]: unknown } | null | undefined): string[] {
  const p = problem?.problems;
  return Array.isArray(p) ? p.filter((x): x is string => typeof x === "string").slice(0, 20) : [];
}

const TRANSITIONS: Record<string, string[]> = {
  draft: ["cancel", "schedule"],
  scheduled: ["cancel", "launch", "unschedule"],
  running: ["cancel", "pause"],
  paused: ["cancel", "resume"],
};

/** Prefer the server's `allowed_actions`; fall back to the documented state machine. */
export function actionsFor(c: Pick<Campaign, "state" | "allowed_actions">): string[] {
  const list = c.allowed_actions && c.allowed_actions.length ? c.allowed_actions : TRANSITIONS[c.state] ?? [];
  return list.filter((a) => ["schedule", "unschedule", "launch", "pause", "resume", "cancel"].includes(a));
}

export const STATE_TONE: Record<string, "neutral" | "ok" | "warn" | "danger" | "brand"> = {
  draft: "neutral",
  scheduled: "brand",
  running: "ok",
  paused: "warn",
  completed: "ok",
  canceled: "neutral",
  failed: "danger",
};

export const ACTION_PERMISSION: Record<string, string> = {
  schedule: "campaigns:manage",
  unschedule: "campaigns:manage",
  pause: "campaigns:manage",
  resume: "campaigns:manage",
  cancel: "campaigns:manage",
  launch: "campaigns:launch",
};

/** Sorted [key, count] pairs with a share of the total, for the report bars. */
export function breakdown(counts: Record<string, number>): { key: string; count: number; share: number }[] {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return Object.entries(counts)
    .map(([key, count]) => ({ key, count, share: total ? count / total : 0 }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

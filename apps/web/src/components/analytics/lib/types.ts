/** Shapes from contracts/openapi/analytics_service.json. */
export type CallsOut = {
  attempted: number;
  answered: number;
  ended: number;
  completed: number;
  failed: number;
  canceled: number;
  opted_out: number;
  qualified: number;
  total_duration_seconds: number;
  average_duration_seconds: number | null;
  answer_rate: number | null;
};

export type EmailsOut = {
  accepted: number;
  delivered: number;
  bounced: number;
  complained: number;
  rejected: number;
  failed: number;
  unsubscribed: number;
  delivery_rate: number | null;
  bounce_rate: number | null;
  complaint_rate: number | null;
};

export type DraftsOut = {
  requested: number;
  generated: number;
  approved: number;
  edited: number;
  rejected: number;
  sent: number;
  approved_rate: number | null;
  edited_rate: number | null;
  rejected_rate: number | null;
};

export type BookingsOut = { created: number; confirmed: number; canceled: number };

export type CostsOut = {
  ai_cost_micros: number;
  ai_input_tokens: number;
  ai_output_tokens: number;
  call_cost_micros: number;
  email_cost_micros: number;
  email_cost_estimated: boolean;
  total_cost_micros: number;
  cost_per_call_micros: number | null;
  cost_per_email_micros: number | null;
  cost_per_ai_interaction_micros: number | null;
};

export type Overview = {
  from: string;
  to: string;
  calls: CallsOut;
  emails: EmailsOut;
  drafts: DraftsOut;
  bookings: BookingsOut;
  costs: CostsOut;
  leads: { created?: number | null };
  campaigns: { launched?: number | null };
};

export type Point = { day: string; value: number };
export type Timeseries = { metric: string; dimension: string; from: string; to: string; points: Point[] };

export type CampaignReport = {
  campaign_id: string;
  channel: string | null;
  launched_at: string | null;
  calls: CallsOut & { cost_micros: number; cost_per_call_micros: number | null };
  emails: { messages: number; accepted: number; delivered: number; bounced: number; complained: number; unsubscribed: number; delivery_rate: number | null };
};

export type AnalyticsExport = {
  id: string;
  kind: string;
  status: "queued" | "processing" | "succeeded" | "failed" | string;
  params: Record<string, unknown>;
  row_count: number | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  download_url?: string | null;
  expires_in_seconds?: number | null;
};

export type TenantUsage = {
  tenant_id: string;
  calls: number;
  call_seconds: number;
  call_cost_micros: number;
  emails_accepted: number;
  email_cost_micros: number;
  ai_requests: number;
  ai_input_tokens: number;
  ai_output_tokens: number;
  ai_cost_micros: number;
  total_cost_micros: number;
};

export type UsageOut = {
  from: string;
  to: string;
  email_cost_estimated: boolean;
  items: TenantUsage[];
  totals: {
    tenants: number;
    calls: number;
    call_seconds: number;
    call_cost_micros: number;
    emails_accepted: number;
    email_cost_micros: number;
    ai_requests: number;
    ai_cost_micros: number;
    total_cost_micros: number;
  };
};

export const EXPORT_TERMINAL = new Set(["succeeded", "failed"]);

/** Response shapes of crm_service (contracts/openapi/crm_service.json). */
export type Items<T> = { items: T[]; next_cursor?: string | null };

export const LEAD_STATUSES = ["new", "contacted", "qualified", "unqualified", "booked", "won", "lost"] as const;
export type LeadStatus = (typeof LEAD_STATUSES)[number];
export const CONTACT_STATUSES = ["active", "inactive", "archived"] as const;

export type ContactSummary = {
  id: string;
  first_name: string | null;
  last_name: string | null;
  company: string | null;
  email: string | null;
  phone_e164: string | null;
  status: string;
};

export type Contact = ContactSummary & {
  country: string | null;
  locale: string | null;
  timezone: string | null;
  source: string | null;
  owner_id: string | null;
  organization_id: string | null;
  custom_fields: Record<string, unknown>;
  tags: string[];
  version: number;
  created_at: string;
  updated_at: string;
};

export type Lead = {
  id: string;
  contact_id: string;
  contact?: ContactSummary | null;
  status: string;
  source: string | null;
  score: number | null;
  qualified: boolean | null;
  owner_id: string | null;
  campaign_id: string | null;
  call_id: string | null;
  booking_id: string | null;
  last_outcome: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Tag = { id: string; name: string; color: string | null; created_at: string };

export type FieldType = "text" | "number" | "boolean" | "date" | "select";
export type FieldDef = {
  id: string;
  key: string;
  label: string;
  type: FieldType;
  options: string[];
  required: boolean;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Organization = { id: string; name: string; domain: string | null; version: number; created_at: string };

export type Consent = {
  id: string;
  contact_id: string;
  channel: string;
  status: string;
  lawful_basis: string;
  source: string;
  source_ref: string | null;
  policy_version: string | null;
  evidence_ref: string | null;
  captured_at: string;
  recorded_by: string;
  created_at: string;
};

export type Note = { id: string; contact_id: string; text: string; source_type: string; source_id: string | null; created_by: string; created_at: string };

export type ChannelState = { allowed: boolean; reasons: string[] };
export type Contactability = { contact_id: string; phone: ChannelState; email: ChannelState };

export type Suppression = {
  id: string;
  channel: string;
  contact_id: string | null;
  reason: string;
  source_type: string;
  value_masked: string | null;
  created_by: string;
  created_at: string;
};

export type ImportJob = {
  id: string;
  status: string;
  filename: string;
  size_bytes: number;
  total_rows: number;
  created_count: number;
  updated_count: number;
  failed_count: number;
  error_code: string | null;
  has_report: boolean;
  created_at: string;
  completed_at: string | null;
};

export type ExportJob = {
  id: string;
  kind: string;
  status: string;
  row_count: number | null;
  error_code: string | null;
  download_url?: string | null;
  expires_in?: number | null;
  created_at: string;
  completed_at: string | null;
};

export type Segment = { id: string; name: string; description: string | null; rules: unknown; version: number; created_at: string; updated_at: string };
export type SegmentPreview = { channel: string | null; matching: number; contactable: number | null };

/** Job states that will not change any more. */
export const TERMINAL_JOB = new Set(["completed", "failed", "canceled", "expired"]);

export function contactName(c: Pick<ContactSummary, "first_name" | "last_name" | "company" | "email" | "phone_e164"> | null | undefined): string {
  if (!c) return "Unknown contact";
  const name = [c.first_name, c.last_name].filter(Boolean).join(" ").trim();
  return name || c.company || c.email || c.phone_e164 || "Unnamed contact";
}

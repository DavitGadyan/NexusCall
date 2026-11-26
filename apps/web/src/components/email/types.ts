/** Response shapes of email-service (contracts/openapi/email_service.json) and inbox mailboxes. */
export type DnsRecord = { name: string; type: string; value: string; purpose: string };

export type Domain = {
  id: string;
  domain: string;
  status: "pending" | "verified" | "failed" | "disabled" | string;
  dkim_status: "pending" | "pass" | "fail" | string;
  spf_status: "pending" | "pass" | "fail" | string;
  dmarc_status: "pending" | "pass" | "fail" | string;
  records: DnsRecord[];
  last_checked_at: string | null;
  verified_at: string | null;
  created_at: string;
  version: number;
};

export type Sender = {
  id: string;
  domain_id: string;
  domain: string;
  domain_status: string;
  from_address: string;
  from_name: string;
  reply_to: string | null;
  status: "active" | "disabled" | string;
  version: number;
  created_at: string;
};

export type TemplateVersion = {
  version: number;
  subject: string;
  html: string;
  text: string;
  variables: string[];
  created_at: string;
  created_by: string | null;
};

export type Template = {
  id: string;
  name: string;
  category: "campaign" | "transactional" | string;
  status: string;
  current_version: number;
  current?: TemplateVersion | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Preview = { version: number; subject: string; html: string; text: string };

export type Campaign = {
  id: string;
  name: string;
  state: "draft" | "scheduled" | "running" | "paused" | "completed" | "canceled" | "failed" | string;
  segment_id: string;
  sender_id: string;
  template_id: string;
  template_version: number | null;
  timezone: string;
  max_per_minute: number;
  scheduled_at: string | null;
  started_at: string | null;
  launched_at: string | null;
  completed_at: string | null;
  ends_at: string | null;
  pause_reason: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  version: number;
};

export type Report = {
  campaign_id: string;
  state: string;
  pause_reason: string | null;
  audience_complete: boolean;
  counts: Record<string, number>;
  recipients: number;
  sent: number;
  rates: Record<string, number>;
};

export type OutboundMessage = {
  id: string;
  campaign_id: string | null;
  contact_id: string | null;
  category: string;
  state: string;
  from_address: string;
  to_masked: string | null;
  attempts: number;
  last_error: string | null;
  created_at: string;
  queued_at: string | null;
  accepted_at: string | null;
  delivered_at: string | null;
  state_changed_at: string;
  provider_message_id: string | null;
  rfc_message_id: string;
};

export type Segment = { id: string; name: string; description: string | null };

export type Mailbox = {
  id: string;
  address: string;
  display_name: string;
  agent_id: string | null;
  auto_draft: boolean;
  status: "active" | "disabled" | string;
  version: number;
  created_at: string;
  updated_at: string;
};

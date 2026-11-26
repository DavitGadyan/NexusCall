/** Response shapes of email-inbox-service (contracts/openapi/email_inbox_service.json). */
export type Attachment = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  scan_status: "clean" | "infected" | "skipped" | "error" | "rejected" | "purged" | string;
  quarantined: boolean;
  downloadable: boolean;
  rejection_reason: string | null;
};

export type InboxMessage = {
  id: string;
  direction: "inbound" | "outbound" | string;
  from_address: string;
  from_name: string;
  to_addresses: string[];
  cc_addresses: string[];
  subject: string;
  received_at: string;
  /** sanitized server-side; still rendered only through SafeHtmlFrame */
  html_sanitized: string | null;
  text_body: string;
  auto_flags: string[];
  content_purged: boolean;
  draft_id: string | null;
  draft_version: number | null;
  attachments?: Attachment[];
};

export type ThreadStatus = "open" | "awaiting_review" | "replied" | "closed";

export type Thread = {
  id: string;
  mailbox_id: string;
  subject: string;
  status: ThreadStatus | string;
  priority: "low" | "normal" | "high" | "urgent" | string;
  intent: string | null;
  assigned_to: string | null;
  contact_id: string | null;
  campaign_id: string | null;
  message_count: number;
  last_message_at: string;
  created_at: string;
};

export type DraftBrief = { id: string; status: string; current_version: number; approved_version: number | null; escalated: boolean };
export type ThreadDetail = Thread & { messages: InboxMessage[]; drafts: DraftBrief[] };

export type Citation = { chunk_id: string; source_id: string; source_name: string };

export type DraftVersion = {
  version: number;
  author_type: "ai" | "user";
  author_id: string;
  ai_generated: boolean;
  body_text: string;
  citations: Citation[];
  quality: { confidence?: number; flags?: string[]; [k: string]: unknown };
  model: string | null;
  provider: string | null;
  prompt_version: number | null;
  created_at: string;
};

export type DraftAction = { action: string; actor_id: string; actor_type: string; created_at: string; reason: string | null; version: number | null };

export type SendCommand = {
  id: string;
  version: number;
  status: "queued" | "sending" | "sent" | "failed" | "blocked" | "canceled" | string;
  to_address: string;
  approved_by: string;
  approved_at: string;
  attempts: number;
  last_error: string | null;
  sent_at: string | null;
  outbound_message_id: string | null;
};

export type DraftStatus = "queued" | "generating" | "pending_review" | "approved" | "sending" | "sent" | "send_failed" | "rejected" | "generation_failed";

export type Draft = {
  id: string;
  thread_id: string;
  message_id: string;
  mailbox_id: string;
  subject: string;
  status: DraftStatus | string;
  priority: string;
  intent: string | null;
  sensitive: boolean;
  escalated: boolean;
  escalation_reason: string | null;
  assigned_to: string | null;
  current_version: number;
  approved_version: number | null;
  approved_by: string | null;
  approved_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type DraftDetail = Draft & {
  summary: string;
  original_message: InboxMessage;
  thread_messages: InboxMessage[];
  versions: DraftVersion[];
  actions: DraftAction[];
  send_commands: SendCommand[];
};

export type ApprovalOut = { draft_id: string; send_command_id: string; status: string; version: number };

export const ESCALATE_REASONS = ["complaint", "legal", "privacy_request", "unsubscribe", "needs_expert", "suspicious", "other"] as const;
export const REJECT_REASONS = ["inaccurate", "inappropriate", "not_needed", "handled_elsewhere", "other"] as const;

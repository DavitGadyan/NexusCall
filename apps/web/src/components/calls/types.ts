/** Response shapes rendered by the call pages (contracts/openapi/call_service.json, voice_service.json). */
export type CallState =
  | "queued"
  | "scheduled"
  | "initiating"
  | "ringing"
  | "connected"
  | "in_progress"
  | "completed"
  | "failed"
  | "canceled"
  | "opted_out";

export type Call = {
  id: string;
  state: CallState | string;
  direction: "inbound" | "outbound" | string;
  is_test: boolean;
  agent_id: string | null;
  campaign_id: string | null;
  contact_id: string | null;
  inbound_number_id: string | null;
  from_masked: string | null;
  to_masked: string | null;
  attempt_count: number;
  blocked_reason: string | null;
  end_reason: string | null;
  handoff_state: string | null;
  opted_out: boolean;
  outcome?: string | null;
  qualified?: boolean | null;
  cost_micros: number;
  duration_seconds: number | null;
  scheduled_for: string | null;
  initiated_at: string | null;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
};

export type HangupOut = { call_id: string; hangup: "requested" | "canceled" | "not_live"; state: string };
export type CreateCallOut = { call_id: string; state: string };

export type InboundNumber = {
  id: string;
  e164: string;
  label: string;
  agent_id: string;
  business_hours_only: boolean;
  handoff_e164: string | null;
  provider_number_sid: string | null;
  use_for_outbound: boolean;
  status: "active" | "disabled" | string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Handoff = { trigger: string; status: string; created_at: string };

export type VoiceSession = {
  call_id: string;
  session_id: string;
  status: string;
  end_reason: string | null;
  turns: number;
  barge_ins: number;
  opted_out: boolean;
  disclosure_played: boolean;
  consent_prompt_played: boolean;
  recording_available: boolean;
  transcript_available: boolean;
  postprocess_status: string;
  started_at: string;
  ended_at: string | null;
  handoffs: Handoff[];
};

export type TranscriptSegment = { seq: number; role: string; text: string; started_ms: number; interrupted: boolean };
export type Transcript = { call_id: string; session_id: string; segments: TranscriptSegment[] };

export type Recording = {
  call_id: string;
  url: string;
  expires_in_seconds: number;
  content_type: string;
  duration_seconds: number;
  channels: number;
};

/** ai_agent_service AgentOut (subset). */
export type Agent = { id: string; name: string; kind: "voice" | "email" | string; status: string; description: string };

/** crm_service ContactOut (subset). */
export type Contact = {
  id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  company: string | null;
  status: string;
};

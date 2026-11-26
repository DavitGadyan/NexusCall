/** Response shapes of ai_agent_service, mcp_registry (tools, grants) and tool_execution (executions). */
export type Agent = {
  id: string;
  name: string;
  description: string;
  kind: "voice" | "email" | string;
  status: string;
  current_version: number | null;
  latest_version: number;
  version: number;
  created_at: string;
  updated_at: string;
};

export type EscalationRule = { trigger: string; action: string; keyword?: string | null };

export type AgentVersion = {
  id: string;
  agent_id: string;
  version: number;
  system_prompt: string;
  greeting: string;
  disclosure_text: string;
  change_note: string;
  model: string | null;
  temperature: number | null;
  voice_id: string | null;
  max_call_seconds: number;
  tool_ids: string[];
  knowledge_source_ids: string[];
  escalation_rules: EscalationRule[];
  created_by: string | null;
  created_at: string;
};

export type Tool = {
  tool_id: string;
  display_name: string;
  description: string;
  kind: string;
  risk_class: number;
  side_effecting: boolean;
  requires_approval: boolean;
  enabled: boolean;
  platform_enabled: boolean;
  rate_per_minute: number;
  reason: string | null;
  version: number;
  input_schema: Record<string, unknown>;
};

export type Grant = {
  tool_id: string;
  risk_class: number;
  requires_approval: boolean;
  effective_requires_approval: boolean;
  calls_per_minute: number | null;
  usable: boolean;
  reason: string | null;
};

export type Execution = {
  id: string;
  tool_id: string;
  tool_version: number | null;
  status: string;
  reason: string | null;
  risk_class: number | null;
  agent_id: string;
  approval_id: string | null;
  call_id: string | null;
  thread_id: string | null;
  contact_id: string | null;
  actor_type: string;
  actor_id: string;
  duration_ms: number;
  output_bytes: number;
  created_at: string;
};

export type Dataset = { id: string; name: string; description: string; created_at: string; updated_at: string };
export type EvalCase = {
  id: string;
  dataset_id: string;
  name: string;
  kind: string;
  source: string;
  input_text: string;
  knowledge_text: string | null;
  expectations: Record<string, unknown>;
  created_at: string;
};
export type EvalResult = { case: string; kind: string; passed: boolean; failed: string[]; flags?: string[]; tool_statuses?: string[] };
export type EvalRun = {
  id: string;
  dataset_id: string;
  agent_id: string;
  prompt_version: number;
  include_security_cases: boolean;
  status: string;
  case_count: number;
  passed: number;
  failed: number;
  error_code: string | null;
  results: EvalResult[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type KnowledgeSource = {
  id: string;
  name: string;
  description: string;
  classification: "public" | "internal" | "confidential" | "restricted" | string;
  origin: "internal" | "external" | string;
  requires_approval: boolean;
  allowed_roles: string[];
  status: string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type KnowledgeDocument = {
  id: string;
  source_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  doc_version: number;
  status: string;
  scan_status: string;
  review_state: string;
  review_reasons: string[];
  pii_detected: boolean;
  chunk_count: number;
  error_code: string | null;
  uploaded_by: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  indexed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type SearchResult = { rank: number; chunk_id: string; document_id: string; source_id: string; source_name: string; text: string; similarity?: number | null };
export type SearchOut = { embedding_model: string; results: SearchResult[] };

export const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"] as const;

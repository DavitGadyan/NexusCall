/** Shapes from contracts/openapi/integration_service.json. Nothing here ever carries a credential value. */
export type Provider = {
  provider: string;
  credential_fields: string[];
  optional_fields: string[];
  config_keys: string[];
  oauth: boolean;
  platform_only: boolean;
  platform_services: string[];
};

export type Connection = {
  id: string;
  provider: string;
  display_name: string | null;
  status: "active" | "disabled" | "revoked" | "error" | string;
  auth_type: "static" | "oauth" | string;
  health: string;
  scope: string;
  owning_service: string;
  vault_ref: string;
  vault_version: number | null;
  account_masked: string | null;
  granted_scopes: string[];
  config: Record<string, string>;
  last_error: string | null;
  last_validated_at: string | null;
  last_rotated_at: string | null;
  next_rotation_due: string | null;
  last_vault_status: string | null;
  last_resolved_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type RotateReason = "planned" | "suspected_compromise" | "provider_requested" | "other";

export type Endpoint = {
  id: string;
  url: string;
  description: string | null;
  event_types: string[];
  status: "active" | "disabled" | "deleted" | string;
  consecutive_failures: number;
  last_delivery_at: string | null;
  secret_ref: string;
  secret_version: number | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Delivery = {
  id: string;
  endpoint_id: string;
  event_id: string;
  event_type: string;
  status: "queued" | "delivering" | "succeeded" | "failed" | "canceled" | string;
  attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  next_attempt_at: string;
  delivered_at: string | null;
  created_at: string;
};

export type Attempt = {
  attempt: number;
  outcome: string;
  status_code: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
};

export type DeliveryDetail = Delivery & { attempt_log: Attempt[] };

export type SecretRef = {
  vault_ref: string;
  scope: "tenant" | "platform" | string;
  tenant_id: string | null;
  connection_id: string | null;
  owning_service: string;
  provider: string;
  kind: string;
  environment: string;
  status: string;
  health: string;
  vault_version: number | null;
  last_vault_status: string | null;
  last_rotated_at: string | null;
  next_rotation_due: string | null;
  last_retrieval_at: string | null;
  last_checked_at: string | null;
};

export type AuditEvent = {
  id: string;
  action: string;
  actor_id: string;
  actor_type: string;
  outcome: string;
  occurred_at: string;
  resource_type: string;
  resource_id: string | null;
  metadata: Record<string, unknown>;
};

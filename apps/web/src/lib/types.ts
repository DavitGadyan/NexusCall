/** Response shapes of the APIs the foundation renders (contracts/openapi/tenant_service.json + briefs). */
export type TenantRef = { id: string; name: string; slug: string };

export type Me = {
  id: string;
  email: string | null;
  name: string | null;
  tenants: TenantRef[];
  platform_permissions: string[];
  mfa: { acr: string | null };
};

export type Access = { tenant_id: string | null; roles: string[]; permissions: string[]; platform_permissions: string[] };

export type Page<T> = { items: T[]; next_cursor?: string | null };

export type Membership = {
  id: string;
  user_id: string;
  email: string;
  display_name: string | null;
  status: "active" | "deactivated";
  team_id: string | null;
  version: number;
  created_at: string;
  last_seen_at: string | null;
  roles: string[];
};

export type TenantRole = {
  key: string;
  name: string;
  description: string;
  requires_mfa: boolean;
  permissions: string[];
  grantable: boolean;
};

export type Team = { id: string; name: string };
export type PermissionInfo = { key: string; description: string };

export type InvitationStatus = "pending" | "sent" | "accepted" | "expired" | "revoked" | "delivery_failed";
export type Invitation = {
  id: string;
  email: string;
  role_keys: string[];
  status: InvitationStatus;
  expires_at: string;
  sent_at: string | null;
  send_count: number;
  delivery_error: string | null;
  created_at: string;
};

export type BulkResultRow = {
  row: number;
  email: string;
  result: "invited" | "error";
  status?: string;
  invitation_id?: string;
  error?: string;
};
export type BulkResult = { summary: { total: number; invited: number; failed: number }; rows: BulkResultRow[] };

export type InvitationPreview = { tenant_name: string; email_masked: string; role_keys: string[]; expires_at: string };

export type SupportGrant = {
  id: string;
  tenant_id: string;
  requested_by: string;
  approved_by: string | null;
  scope: "read_config" | "read_operations" | "read_crm";
  reason: string;
  ticket_ref: string;
  status: "pending" | "active" | "rejected" | "revoked" | "expired";
  expires_at: string;
  revoked_at: string | null;
  created_at: string;
  updated_at: string;
};

export type SettingsSection = Record<string, unknown> & { _version: number };

export type AuditEvent = {
  id: string;
  occurred_at: string;
  tenant_id?: string | null;
  actor_type: string;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  request_id: string | null;
  correlation_id: string | null;
  producer: string;
  metadata: Record<string, unknown> | null;
};

export type Notification = {
  id: string;
  title: string;
  body: string;
  link: string | null;
  kind: string;
  created_at: string;
  read_at: string | null;
};
export type NotificationPage = Page<Notification> & { unread_count: number };

export type Tenant = {
  id: string;
  slug: string;
  name: string;
  status: "active" | "suspended" | "archived";
  data_region: string;
  created_by: string | null;
  status_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  members?: number;
};

export type TenantDetail = Tenant & {
  members: { id: string; email: string; display_name: string | null; status: string }[];
  open_invitations: number;
  support_grants: SupportGrant[];
};

export type PlatformUser = {
  id: string;
  email: string;
  display_name: string | null;
  status: "active" | "deactivated";
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
  tenants: number;
  platform_roles: string[];
};

export type PlatformUserDetail = Omit<PlatformUser, "tenants"> & {
  memberships: { id: string; name: string; status: string; membership: string }[];
  platform_roles: string[];
  idp: { enabled: boolean; email_verified: boolean; mfa_enrolled: boolean; realm_roles: string[] } | null;
  sessions: { id: string; ip: string | null; started: number; last_access: number; clients: string[] }[];
};

export type EffectivePermissions = { user_id: string; tenant_id: string; roles: string[]; permissions: string[] };

export type CatalogueRole = {
  key: string;
  name: string;
  scope: "tenant" | "platform";
  description: string;
  is_system: boolean;
  requires_mfa: boolean;
  permissions: string[];
};
export type IamRoles = { roles: CatalogueRole[]; catalogue: { tenant: Record<string, string>; platform: Record<string, string> } };

export type ServiceAccount = {
  id: string;
  name: string;
  client_id: string;
  owner_user_id: string;
  purpose: string;
  tenant_id: string | null;
  scopes: string[];
  status: "active" | "disabled";
  review_at: string;
  secret_ref: string | null;
  last_rotated_at: string | null;
  created_at: string;
};

export type PrivilegedRequest = {
  id: string;
  kind: string;
  payload: { user_id?: string; role_key?: string; [k: string]: unknown };
  reason: string;
  requested_by: string;
  approved_by: string | null;
  status: "pending" | "approved" | "rejected" | "expired";
  expires_at: string;
  created_at: string;
};

export type AuthEvent = {
  time: number | null;
  type: string | null;
  user_id: string | null;
  client_id: string | null;
  ip: string | null;
  error: string | null;
};

export type IdpStatus = {
  issuer?: string;
  audience?: string;
  allowed_algorithms?: string[];
  reachable?: boolean;
  error?: string;
  realm?: string;
  brute_force_protected?: boolean;
  otp_policy?: string;
  access_token_lifespan?: number;
  revoke_refresh_token?: boolean;
  password_policy?: string;
  signing_keys?: { kid: string; alg: string }[];
};

export type Overview = {
  tenants: Record<string, number>;
  users: number;
  active_memberships: number;
  pending_privileged_requests: number;
  last_rls_scan: { at: string; summary: RlsSummary } | null;
  kill_switches: { calls_enabled: boolean; emails_enabled: boolean; ai_enabled: boolean; reason?: string; _version?: number };
  maintenance: { mode: string; message: string; _version?: number };
};

export type RlsTable = {
  table: string;
  schema: string;
  status: string;
  rls_enabled: boolean;
  rls_forced: boolean;
  has_tenant_id: boolean;
  policies: string[];
  commands: string[];
};

export type RlsServiceResult = {
  service: string;
  available: boolean;
  error?: string;
  database?: string;
  app_role?: string;
  role_issues?: string[];
  tables: RlsTable[];
};

export type RlsSummary = {
  services_scanned: number;
  services_unreachable: number;
  tables: number;
  issues: number;
  role_issues: number;
};

export type RlsScan = { id: string; started_by: string | null; created_at: string; summary: RlsSummary; results: RlsServiceResult[] };

export type FeatureFlag = {
  key: string;
  description: string;
  enabled: boolean;
  tenant_overridable: boolean;
  environments: string[];
  created_at: string;
  updated_at: string;
};

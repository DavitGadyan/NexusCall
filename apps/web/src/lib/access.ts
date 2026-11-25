/**
 * UI permission gating for shells and actions. The backend is authoritative; these only decide what to show.
 */
import { hasPermission } from "./permissions";

/** Any of these opens the tenant admin console. */
export const TENANT_ADMIN_ENTRY = ["tenant:manage", "tenant:users:manage", "audit:read"] as const;

export function canAny(granted: ReadonlySet<string> | readonly string[], required: readonly string[]): boolean {
  return required.some((r) => hasPermission(granted, r));
}

export function canAccessTenantAdmin(tenantPermissions: readonly string[]): boolean {
  return canAny(tenantPermissions, TENANT_ADMIN_ENTRY);
}

/** Platform access needs real `platform:*` permissions (token role AND DB assignment, resolved server-side). */
export function canAccessPlatform(platformPermissions: readonly string[]): boolean {
  return platformPermissions.some((p) => p.startsWith("platform:"));
}

export type Surface = "workspace" | "tenant-admin" | "platform-admin";

export function availableSurfaces(input: {
  hasTenant: boolean;
  tenantPermissions: readonly string[];
  platformPermissions: readonly string[];
}): Surface[] {
  const out: Surface[] = [];
  if (input.hasTenant) out.push("workspace");
  if (input.hasTenant && canAccessTenantAdmin(input.tenantPermissions)) out.push("tenant-admin");
  if (canAccessPlatform(input.platformPermissions)) out.push("platform-admin");
  return out;
}

/** Where to land after sign-in when no returnTo was requested. */
export function defaultLanding(input: { tenantCount: number; platformPermissions: readonly string[] }): string {
  if (input.tenantCount > 0) return "/app";
  if (canAccessPlatform(input.platformPermissions)) return "/platform";
  return "/app";
}

/** Mirrors nexus_core.authz.require_step_up: acr aal2+ and an authentication within `maxAgeSeconds`. */
export function hasRecentStepUp(acr: string | null, authTime: number | null, nowMs: number, maxAgeSeconds = 300): boolean {
  if (!acr || !["aal2", "aal3", "2"].includes(acr)) return false;
  if (authTime === null) return false;
  return nowMs / 1000 - authTime <= maxAgeSeconds;
}

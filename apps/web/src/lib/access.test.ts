import { describe, expect, it } from "vitest";
import {
  availableSurfaces,
  canAccessPlatform,
  canAccessTenantAdmin,
  canAny,
  defaultLanding,
  hasRecentStepUp,
} from "./access";
import { filterNav, platformNav, tenantAdminNav, workspaceNav } from "./nav";
import { hasPermission } from "./permissions";

const ANALYST = ["bookings:read", "ai:usage:read", "leads:read", "campaigns:read", "calls:read", "analytics:read"];
const TENANT_ADMIN = ["tenant:manage", "tenant:users:manage", "audit:read", "integrations:manage", "leads:read"];
const OPERATOR = ["platform:overview:read", "platform:tenants:read", "platform:support:request", "platform:security:read"];

describe("permission gating", () => {
  it("tenant admin console needs an admin permission", () => {
    expect(canAccessTenantAdmin(TENANT_ADMIN)).toBe(true);
    expect(canAccessTenantAdmin(ANALYST)).toBe(false);
    expect(canAccessTenantAdmin([])).toBe(false);
  });

  it("platform console needs platform permissions; tenant permissions never count", () => {
    expect(canAccessPlatform(OPERATOR)).toBe(true);
    expect(canAccessPlatform([])).toBe(false);
    expect(canAccessPlatform(TENANT_ADMIN)).toBe(false);
  });

  it("a tenant admin is offered workspace + tenant admin, never the platform", () => {
    expect(availableSurfaces({ hasTenant: true, tenantPermissions: TENANT_ADMIN, platformPermissions: [] })).toEqual([
      "workspace",
      "tenant-admin",
    ]);
    expect(availableSurfaces({ hasTenant: false, tenantPermissions: [], platformPermissions: OPERATOR })).toEqual([
      "platform-admin",
    ]);
  });

  it("filters navigation by permission", () => {
    const can = (p: ReadonlySet<string>, r: string) => hasPermission(p, r);
    const analystNav = filterNav(workspaceNav, new Set(ANALYST), can).flatMap((s) => s.items.map((i) => i.href));
    expect(analystNav).toContain("/app/calls");
    expect(analystNav).not.toContain("/app/inbox");
    const adminNav = filterNav(tenantAdminNav, new Set(TENANT_ADMIN), can).flatMap((s) => s.items.map((i) => i.href));
    expect(adminNav).toEqual(expect.arrayContaining(["/admin/team", "/admin/invitations", "/admin/settings", "/admin/audit"]));
    expect(adminNav).not.toContain("/admin/email");
    const opNav = filterNav(platformNav, new Set(OPERATOR), can).flatMap((s) => s.items.map((i) => i.href));
    expect(opNav).toContain("/platform/security/rls");
    expect(opNav).not.toContain("/platform/service-accounts");
    expect(filterNav(platformNav, new Set(TENANT_ADMIN), can)).toEqual([]);
  });

  it("supports wildcard grants", () => {
    expect(canAny(["platform:*"], ["platform:tenants:manage"])).toBe(true);
    expect(canAny(["emails:drafts:review"], ["emails:drafts:approve", "tenant:manage"])).toBe(false);
  });

  it("lands users on the right surface", () => {
    expect(defaultLanding({ tenantCount: 2, platformPermissions: OPERATOR })).toBe("/app");
    expect(defaultLanding({ tenantCount: 0, platformPermissions: OPERATOR })).toBe("/platform");
    expect(defaultLanding({ tenantCount: 0, platformPermissions: [] })).toBe("/app");
  });

  it("mirrors the backend step-up rule", () => {
    const now = 1_700_000_000_000;
    const t = now / 1000;
    expect(hasRecentStepUp("aal2", t - 60, now)).toBe(true);
    expect(hasRecentStepUp("aal2", t - 301, now)).toBe(false);
    expect(hasRecentStepUp("aal1", t, now)).toBe(false);
    expect(hasRecentStepUp(null, t, now)).toBe(false);
    expect(hasRecentStepUp("aal2", null, now)).toBe(false);
  });
});

/**
 * Server-side shell for the three surfaces. Validates the session, loads the user and permissions,
 * filters navigation and shows access-denied / no-workspace states. UI gating only: every API call is
 * authorised again by the services.
 */
import { redirect } from "next/navigation";
import * as React from "react";
import { availableSurfaces, canAccessPlatform, canAccessTenantAdmin, hasRecentStepUp, type Surface } from "@/lib/access";
import { filterNav, platformNav, tenantAdminNav, workspaceNav } from "@/lib/nav";
import { hasPermission } from "@/lib/permissions";
import { ErrorState } from "@/components/ui/states";
import { loadShell, loginUrlFor } from "@/lib/server/shell";
import { AppShell } from "./AppShell";
import { AccessDenied, NoWorkspace, SignedInProblem } from "./SurfaceStates";
import { ShellProviders } from "./ShellProviders";

const can = (granted: ReadonlySet<string>, required: string) => hasPermission(granted, required);

export async function ShellLayout({ surface, children }: { surface: Surface; children: React.ReactNode }) {
  const outcome = await loadShell();
  if (outcome.kind === "signed_out") redirect(await loginUrlFor());
  if (outcome.kind === "blocked") return <SignedInProblem problem={outcome.problem} csrfToken={outcome.csrf} />;

  const { ctx } = outcome;
  const { me, access, active } = ctx;
  const tenantPerms = access?.permissions ?? [];
  const platformPerms = me.platform_permissions ?? [];
  const surfaces = availableSurfaces({
    hasTenant: Boolean(ctx.tenantId && access),
    tenantPermissions: tenantPerms,
    platformPermissions: platformPerms,
  });

  const sections =
    surface === "platform-admin"
      ? filterNav(platformNav, new Set(platformPerms), can)
      : surface === "tenant-admin"
        ? filterNav(tenantAdminNav, new Set(tenantPerms), can)
        : filterNav(workspaceNav, new Set(tenantPerms), can);

  let content: React.ReactNode = children;
  if (surface === "platform-admin") {
    if (!canAccessPlatform(platformPerms)) content = <AccessDenied surface="platform" />;
  } else if (me.tenants.length === 0) {
    content = <NoWorkspace canOpenPlatform={canAccessPlatform(platformPerms)} />;
  } else if (!access) {
    content = <ErrorState title="This workspace is unavailable" problem={ctx.accessProblem} />;
  } else if (surface === "tenant-admin" && !canAccessTenantAdmin(tenantPerms)) {
    content = <AccessDenied surface="tenant-admin" />;
  }

  const session = active.session;
  return (
    <ShellProviders
      session={{
        user: { id: me.id, email: me.email, name: me.name },
        acr: session.acr,
        authTime: session.authTime,
        tenants: me.tenants,
        tenantId: ctx.tenantId,
        roles: access?.roles ?? [],
        permissions: tenantPerms,
        platformPermissions: platformPerms,
      }}
    >
      <AppShell
        surface={surface}
        sections={sections}
        user={{ name: me.name ?? me.email ?? "Signed in", email: me.email ?? "" }}
        tenants={me.tenants}
        activeTenantId={ctx.tenantId}
        surfaces={surfaces}
        csrfToken={session.csrf}
        mfaVerified={hasRecentStepUp(session.acr, session.authTime, Date.now(), 3600)}
      >
        {content}
      </AppShell>
    </ShellProviders>
  );
}

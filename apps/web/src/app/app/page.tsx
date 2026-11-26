import Link from "next/link";
import { canAccessTenantAdmin } from "@/lib/access";
import { filterNav, workspaceNav } from "@/lib/nav";
import { hasPermission } from "@/lib/permissions";
import { Card, CardHeader, EmptyState, PageHeader } from "@/components/ui";
import { loadShell } from "@/lib/server/shell";
import { WorkspaceDashboard } from "@/components/dashboard/WorkspaceDashboard";

export const metadata = { title: "Workspace" };

export default async function WorkspaceHome() {
  const outcome = await loadShell();
  if (outcome.kind !== "ok" || !outcome.ctx.access) return null;
  const { me, access, tenantId } = outcome.ctx;
  const tenant = me.tenants.find((t) => t.id === tenantId);
  const perms = new Set(access.permissions);
  const sections = filterNav(workspaceNav, perms, (p, r) => hasPermission(p, r));
  return (
    <>
      <PageHeader
        title={tenant ? tenant.name : "Workspace"}
        description={`Signed in as ${me.email ?? me.name ?? "you"}. Your roles here: ${access.roles.join(", ") || "none"}.`}
      />
      {hasPermission(access.permissions, "analytics:read") ? <WorkspaceDashboard /> : null}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {sections.map((s) => (
          <Card key={s.label}>
            <CardHeader title={s.label} />
            <ul className="flex flex-col px-5 py-3 text-sm">
              {s.items.map((i) => (
                <li key={i.href}>
                  <Link className="block py-1 text-brand hover:underline" href={i.href}>
                    {i.label}
                  </Link>
                </li>
              ))}
            </ul>
          </Card>
        ))}
        {canAccessTenantAdmin(access.permissions) ? (
          <Card>
            <CardHeader title="Administration" description="Team, invitations, settings and audit for this workspace." />
            <div className="px-5 py-3 text-sm">
              <Link className="text-brand hover:underline" href="/admin">
                Open workspace administration
              </Link>
            </div>
          </Card>
        ) : null}
      </div>
      {sections.length === 0 ? (
        <Card>
          <EmptyState title="Nothing to show yet" description="Your roles do not include any workspace modules. Ask an administrator for access." />
        </Card>
      ) : null}
    </>
  );
}

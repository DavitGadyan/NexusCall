import Link from "next/link";
import * as React from "react";
import type { NavSection } from "@/lib/nav";
import { NavLinks } from "./NavLinks";

export type ShellUser = { name: string; email: string };
export type ShellTenant = { id: string; name: string };

export function AppShell({
  surface, sections, user, tenants, activeTenantId, switcher, children,
}: {
  surface: "workspace" | "tenant-admin" | "platform-admin";
  sections: NavSection[];
  user: ShellUser;
  tenants: ShellTenant[];
  activeTenantId: string | null;
  switcher?: React.ReactNode;
  children: React.ReactNode;
}) {
  const surfaceLabel = { workspace: "Workspace", "tenant-admin": "Tenant admin", "platform-admin": "Platform admin" }[surface];
  const active = tenants.find((t) => t.id === activeTenantId);
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface md:flex">
        <div className="flex h-14 items-center gap-2 border-b border-border px-4">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-brand text-xs font-bold text-brand-fg">NC</span>
          <div className="leading-tight">
            <p className="text-sm font-semibold">NexusCall</p>
            <p className="text-xs text-muted">{surfaceLabel}</p>
          </div>
        </div>
        <nav aria-label={`${surfaceLabel} navigation`} className="flex-1 overflow-y-auto px-2 py-3">
          <NavLinks sections={sections} />
        </nav>
        <div className="border-t border-border p-3 text-xs text-muted">
          <SurfaceLinks surface={surface} />
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-4 border-b border-border bg-surface px-4 md:px-6">
          <div className="min-w-0 text-sm">
            {switcher ?? <span className="font-medium">{active?.name ?? "No tenant selected"}</span>}
          </div>
          <div className="flex items-center gap-3 text-sm">
            <div className="hidden text-right leading-tight sm:block">
              <p className="font-medium">{user.name}</p>
              <p className="text-xs text-muted">{user.email}</p>
            </div>
            <form action="/api/auth/logout" method="post">
              <button className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2" type="submit">
                Sign out
              </button>
            </form>
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}

function SurfaceLinks({ surface }: { surface: string }) {
  return (
    <div className="flex flex-col gap-1">
      {surface !== "workspace" && <Link className="hover:text-fg" href="/app">→ Workspace</Link>}
      {surface !== "tenant-admin" && <Link className="hover:text-fg" href="/admin/team">→ Tenant admin</Link>}
      {surface !== "platform-admin" && <Link className="hover:text-fg" href="/platform">→ Platform admin</Link>}
    </div>
  );
}

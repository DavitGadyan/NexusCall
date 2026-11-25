import { ShieldCheck } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import type { Surface } from "@/lib/access";
import type { NavSection } from "@/lib/nav";
import { MobileNav } from "./MobileNav";
import { NavLinks } from "./NavLinks";
import { NotificationBell } from "./NotificationBell";
import { TenantSwitcher } from "./TenantSwitcher";

export type ShellUser = { name: string; email: string };
export type ShellTenant = { id: string; name: string };

const SURFACE_LABEL: Record<Surface, string> = {
  workspace: "Workspace",
  "tenant-admin": "Tenant admin",
  "platform-admin": "Platform admin",
};
const SURFACE_HOME: Record<Surface, string> = { workspace: "/app", "tenant-admin": "/admin", "platform-admin": "/platform" };

export function AppShell({
  surface,
  sections,
  user,
  tenants,
  activeTenantId,
  surfaces,
  csrfToken,
  mfaVerified,
  notifications = true,
  children,
}: {
  surface: Surface;
  sections: NavSection[];
  user: ShellUser;
  tenants: ShellTenant[];
  activeTenantId: string | null;
  /** surfaces this user may open (permission-aware) */
  surfaces: Surface[];
  csrfToken: string;
  mfaVerified: boolean;
  notifications?: boolean;
  children: React.ReactNode;
}) {
  const surfaceLabel = SURFACE_LABEL[surface];
  const others = surfaces.filter((s) => s !== surface);
  const brand = (
    <div className="flex h-14 items-center gap-2 border-b border-border px-4">
      <span className="grid h-7 w-7 place-items-center rounded-md bg-brand text-xs font-bold text-brand-fg" aria-hidden>
        NC
      </span>
      <div className="leading-tight">
        <p className="text-sm font-semibold">NexusCall</p>
        <p className="text-xs text-muted">{surfaceLabel}</p>
      </div>
    </div>
  );
  const switchLinks =
    others.length > 0 ? (
      <div className="flex flex-col gap-1 border-t border-border p-3 text-xs text-muted">
        {others.map((s) => (
          <Link key={s} className="rounded px-1 py-0.5 hover:text-fg" href={SURFACE_HOME[s]}>
            → {SURFACE_LABEL[s]}
          </Link>
        ))}
      </div>
    ) : null;

  return (
    <div className="flex min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface md:flex">
        {brand}
        <nav aria-label={`${surfaceLabel} navigation`} className="flex-1 overflow-y-auto px-2 py-3">
          <NavLinks sections={sections} />
        </nav>
        {switchLinks}
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-3 border-b border-border bg-surface px-3 md:px-6">
          <div className="flex min-w-0 items-center gap-2 text-sm">
            <MobileNav label={`${surfaceLabel} navigation`} sections={sections} others={others.map((s) => ({ label: SURFACE_LABEL[s], href: SURFACE_HOME[s] }))} />
            {surface === "platform-admin" ? (
              <span className="truncate font-medium">Platform control plane</span>
            ) : (
              <TenantSwitcher tenants={tenants} activeTenantId={activeTenantId} />
            )}
          </div>
          <div className="flex items-center gap-2 text-sm sm:gap-3">
            {notifications && activeTenantId ? <NotificationBell /> : null}
            {mfaVerified ? (
              <span className="hidden items-center gap-1 rounded-full bg-ok/10 px-2 py-0.5 text-xs font-medium text-ok sm:inline-flex" title="Recently verified with MFA">
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> MFA verified
              </span>
            ) : null}
            <div className="hidden text-right leading-tight sm:block">
              <p className="font-medium">{user.name}</p>
              <p className="text-xs text-muted">{user.email}</p>
            </div>
            <form action="/api/auth/logout" method="post">
              <input type="hidden" name="csrf_token" value={csrfToken} />
              <button className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2" type="submit">
                Sign out
              </button>
            </form>
          </div>
        </header>
        <main id="main" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 md:px-6">
          {children}
        </main>
      </div>
    </div>
  );
}

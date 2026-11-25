"use client";
/**
 * Tenant (workspace) switcher. The BFF validates the choice against the user's memberships; afterwards the
 * page reloads completely so no client state from the previous tenant survives (build.md §8.2).
 */
import { Building2 } from "lucide-react";
import * as React from "react";
import { readCsrfToken } from "@/lib/client/api";
import { useToast } from "@/components/ui/toast";

export function TenantSwitcher({ tenants, activeTenantId }: { tenants: { id: string; name: string }[]; activeTenantId: string | null }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  const active = tenants.find((t) => t.id === activeTenantId);

  if (tenants.length === 0) return <span className="text-muted">No workspace</span>;
  if (tenants.length === 1) {
    return (
      <span className="flex min-w-0 items-center gap-2 font-medium">
        <Building2 className="h-4 w-4 shrink-0 text-muted" aria-hidden />
        <span className="truncate">{active?.name ?? tenants[0]?.name}</span>
      </span>
    );
  }

  async function onChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const tenantId = e.target.value;
    if (tenantId === activeTenantId) return;
    setBusy(true);
    try {
      const resp = await fetch("/api/session/tenant", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() ?? "" },
        body: JSON.stringify({ tenant_id: tenantId }),
        credentials: "same-origin",
      });
      if (!resp.ok) throw new Error("switch failed");
      window.location.assign("/app");
    } catch {
      setBusy(false);
      toast({ tone: "error", title: "Could not switch workspace", description: "Reload the page and try again." });
    }
  }

  return (
    <label className="flex min-w-0 items-center gap-2">
      <Building2 className="h-4 w-4 shrink-0 text-muted" aria-hidden />
      <span className="sr-only">Workspace</span>
      <select
        value={activeTenantId ?? ""}
        onChange={onChange}
        disabled={busy}
        data-testid="tenant-switcher"
        className="h-8 max-w-[14rem] truncate rounded-md border border-border bg-surface px-2 text-sm font-medium focus:border-brand focus:outline-none"
      >
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
    </label>
  );
}

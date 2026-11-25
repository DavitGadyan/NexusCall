"use client";
/** Tenant view of platform support access to this workspace (build.md §18.1, §20.3): always visible, read-only. */
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import type { Page, SupportGrant } from "@/lib/types";
import { Card, CardHeader, type Column, DataTable, EmptyState, StatusBadge } from "@/components/ui";

export const SCOPE_LABEL: Record<SupportGrant["scope"], string> = {
  read_config: "Read configuration",
  read_operations: "Read operations (campaigns, calls)",
  read_crm: "Read CRM",
};

export function SupportAccess() {
  const grants = useApi<Page<SupportGrant>>("/api/v1/support-grants");
  const columns: Column<SupportGrant>[] = [
    { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
    { key: "scope", header: "Scope", cell: (g) => SCOPE_LABEL[g.scope] ?? g.scope },
    { key: "reason", header: "Reason", cell: (g) => <span className="line-clamp-2 max-w-md">{g.reason}</span> },
    { key: "ticket", header: "Ticket", cell: (g) => <span className="font-mono text-xs">{g.ticket_ref}</span>, hideOnMobile: true },
    { key: "requested", header: "Requested by", cell: (g) => <span className="font-mono text-xs" title={g.requested_by}>{shortId(g.requested_by)}</span>, hideOnMobile: true },
    { key: "approved", header: "Approved by", cell: (g) => <span className="font-mono text-xs" title={g.approved_by ?? ""}>{shortId(g.approved_by)}</span>, hideOnMobile: true },
    {
      key: "expires",
      header: "Expires",
      cell: (g) => <span title={formatDateTime(g.expires_at)}>{["pending", "active"].includes(g.status) ? relativeTime(g.expires_at) : formatDateTime(g.expires_at)}</span>,
    },
  ];
  return (
    <Card>
      <CardHeader
        title="Support access grants"
        description="Platform staff can only read your data through a time-limited, reason-coded grant approved by a second platform administrator. Every read is audited and grants end automatically."
      />
      <DataTable
        caption="Support access grants"
        columns={columns}
        rows={grants.data?.items ?? []}
        rowKey={(g) => g.id}
        loading={grants.loading}
        error={grants.error}
        onRetry={grants.reload}
        empty={<EmptyState title="No support access" description="Nobody from the platform team has requested access to this workspace." />}
      />
    </Card>
  );
}

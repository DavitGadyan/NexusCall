"use client";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import type { Page, Tenant } from "@/lib/types";
import { ButtonLink, Card, CardHeader, type Column, DataTable, EmptyState, Input, Select, StatusBadge } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";

export function TenantDirectory() {
  const can = useCan();
  const [q, setQ] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [status, setStatus] = React.useState("");
  const list = useApi<Page<Tenant>>(withQuery("/api/v1/platform/tenants", { q: query, status }));

  React.useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const columns: Column<Tenant>[] = [
    {
      key: "name",
      header: "Tenant",
      cell: (t) => (
        <Link href={`/platform/tenants/${t.id}`} className="group block min-w-0">
          <span className="block truncate font-medium text-brand group-hover:underline">{t.name}</span>
          <span className="block truncate font-mono text-xs text-muted">{t.slug}</span>
        </Link>
      ),
    },
    { key: "status", header: "Status", cell: (t) => <StatusBadge status={t.status} /> },
    { key: "members", header: "Members", cell: (t) => <span className="tabular">{t.members ?? 0}</span> },
    { key: "region", header: "Data region", cell: (t) => t.data_region, hideOnMobile: true },
    { key: "created", header: "Created", cell: (t) => formatDateTime(t.created_at), hideOnMobile: true },
  ];

  return (
    <Card>
      <CardHeader
        title="Tenant directory"
        actions={can("platform:tenants:manage") ? <ButtonLink variant="primary" size="sm" href="/platform/tenants/new">Create tenant</ButtonLink> : null}
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
        <label className="sr-only" htmlFor="tenant-q">
          Search tenants
        </label>
        <Input id="tenant-q" placeholder="Search name or slug" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
        <label className="sr-only" htmlFor="tenant-status">
          Status
        </label>
        <Select id="tenant-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-40">
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="suspended">Suspended</option>
          <option value="archived">Archived</option>
        </Select>
      </div>
      <DataTable
        caption="Tenants"
        columns={columns}
        rows={list.data?.items ?? []}
        rowKey={(t) => t.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        empty={<EmptyState title={query || status ? "No matching tenants" : "No tenants yet"} description="Create a tenant and invite its owner to get started." />}
      />
    </Card>
  );
}

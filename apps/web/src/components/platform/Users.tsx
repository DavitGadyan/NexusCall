"use client";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { relativeTime } from "@/lib/format";
import type { Page, PlatformUser } from "@/lib/types";
import { Badge, Card, CardHeader, type Column, DataTable, EmptyState, Input, StatusBadge } from "@/components/ui";

export function UserDirectory() {
  const [q, setQ] = React.useState("");
  const [query, setQuery] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);
  const list = useApi<Page<PlatformUser>>(withQuery("/api/v1/platform/users", { q: query }));
  const columns: Column<PlatformUser>[] = [
    {
      key: "user",
      header: "User",
      cell: (u) => (
        <Link href={`/platform/users/${u.id}`} className="group block min-w-0">
          <span className="block truncate font-medium text-brand group-hover:underline">{u.display_name ?? u.email}</span>
          <span className="block truncate text-xs text-muted">{u.email}</span>
        </Link>
      ),
    },
    { key: "status", header: "Status", cell: (u) => <StatusBadge status={u.status} /> },
    { key: "tenants", header: "Tenants", cell: (u) => <span className="tabular">{u.tenants}</span> },
    {
      key: "platform",
      header: "Platform roles",
      cell: (u) => (u.platform_roles.length ? <div className="flex flex-wrap gap-1">{u.platform_roles.map((r) => <Badge key={r} tone="brand">{r}</Badge>)}</div> : <span className="text-muted">—</span>),
      hideOnMobile: true,
    },
    { key: "seen", header: "Last seen", cell: (u) => relativeTime(u.last_seen_at), hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader title="Users" description="Human identities. Service identities are listed under Service accounts." />
      <div className="border-b border-border px-4 py-3">
        <label className="sr-only" htmlFor="user-q">
          Search users
        </label>
        <Input id="user-q" placeholder="Search email or name" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
      </div>
      <DataTable
        caption="Users"
        columns={columns}
        rows={list.data?.items ?? []}
        rowKey={(u) => u.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        empty={<EmptyState title={query ? "No matching users" : "No users yet"} />}
      />
    </Card>
  );
}

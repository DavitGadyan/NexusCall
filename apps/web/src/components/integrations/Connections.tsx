"use client";
/**
 * Provider connections for this workspace: masked account, health, last validation and rotation due.
 * No column ever holds a credential (the API has none to give). Shows the OAuth callback result when the
 * provider redirects back here.
 */
import { Plus } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { useLiveEvents } from "@/lib/client/use-live-events";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Card, CardHeader, type Column, DataTable, EmptyState, Notice, Select } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { healthInfo, parseOAuthReturn, PROVIDER_LABELS, providerLabel, rotationStatus } from "./lib/connections";
import type { Connection } from "./lib/types";

export function Connections({
  basePath,
  newHref,
  oauthQuery,
}: {
  basePath: string;
  newHref: string | null;
  /** the OAuth callback redirect's query (`oauth`, `provider`, `connection`), passed by the page */
  oauthQuery?: Record<string, string>;
}) {
  const can = useCan();
  const oauth = React.useMemo(() => parseOAuthReturn(new URLSearchParams(oauthQuery ?? {})), [oauthQuery]);
  const [provider, setProvider] = React.useState("");
  const [status, setStatus] = React.useState("");
  const list = useCursorList<Connection>(withQuery("/api/v1/integrations/connections", { provider, status }));
  const reloadRef = React.useRef(list.reload);
  reloadRef.current = list.reload;
  const debounce = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  useLiveEvents(
    () => {
      if (debounce.current) return;
      debounce.current = setTimeout(() => {
        debounce.current = null;
        reloadRef.current();
      }, 1500);
    },
    { events: ["integration.*"] },
  );
  const now = Date.now();
  const canManage = can("integrations:manage");

  const columns: Column<Connection>[] = [
    {
      key: "provider",
      header: "Provider",
      cell: (c) => (
        <Link href={`${basePath}/${c.id}`} className="font-medium text-brand hover:underline">
          {c.display_name || providerLabel(c.provider)}
          <span className="block text-xs font-normal text-muted">
            {providerLabel(c.provider)}
            {c.scope !== "default" ? ` · ${c.scope}` : ""}
            {c.auth_type === "oauth" ? " · OAuth" : ""}
          </span>
        </Link>
      ),
    },
    { key: "account", header: "Account", cell: (c) => <span className="font-mono text-xs">{c.account_masked ?? "—"}</span> },
    {
      key: "health",
      header: "Health",
      cell: (c) => {
        const h = healthInfo(c.health);
        return (
          <span title={h.help}>
            <Badge tone={h.tone}>{h.label}</Badge>
          </span>
        );
      },
    },
    { key: "validated", header: "Last validated", hideOnMobile: true, cell: (c) => <span title={formatDateTime(c.last_validated_at)}>{relativeTime(c.last_validated_at, now)}</span> },
    {
      key: "rotation",
      header: "Rotation due",
      hideOnMobile: true,
      cell: (c) => {
        if (c.status === "revoked") return "—";
        const r = rotationStatus(c.next_rotation_due, now);
        return (
          <span title={formatDateTime(c.next_rotation_due)}>
            <Badge tone={r.tone}>{r.label}</Badge>
          </span>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {oauth ? (
        <Notice
          tone={oauth.tone === "ok" ? "ok" : oauth.tone}
          title={oauth.title}
          action={
            oauth.connectionId ? (
              <Link href={`${basePath}/${oauth.connectionId}`} className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-2">
                Open connection
              </Link>
            ) : null
          }
        />
      ) : null}
      <Card>
        <CardHeader
          title="Provider connections"
          description="Credentials are write-only. Test, rotate, disable or revoke a connection from its page."
          actions={
            newHref && canManage ? (
              <Link href={newHref} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand px-4 text-sm font-medium text-brand-fg hover:bg-brand/90" data-testid="new-connection">
                <Plus className="h-4 w-4" aria-hidden /> Add connection
              </Link>
            ) : null
          }
        />
        <div className="flex flex-wrap gap-3 border-b border-border px-5 py-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Provider</span>
            <Select value={provider} onChange={(e) => setProvider(e.target.value)} className="w-48">
              <option value="">All</option>
              {Object.keys(PROVIDER_LABELS)
                .filter((p) => !p.endsWith("_oauth_client"))
                .map((p) => (
                  <option key={p} value={p}>
                    {providerLabel(p)}
                  </option>
                ))}
            </Select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Status</span>
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-40">
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="error">Error</option>
              <option value="disabled">Disabled</option>
              <option value="revoked">Revoked</option>
            </Select>
          </label>
        </div>
        <DataTable
          caption="Provider connections"
          columns={columns}
          rows={list.items}
          rowKey={(c) => c.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={
            <EmptyState
              title={provider || status ? "No connections match" : "No providers connected"}
              description={canManage ? "Add a connection to use your own telephony, email, calendar or CRM accounts." : "An administrator can connect providers for this workspace."}
              action={
                newHref && canManage && !provider && !status ? (
                  <Link href={newHref} className="text-sm font-medium text-brand hover:underline">
                    Add connection
                  </Link>
                ) : null
              }
            />
          }
        />
      </Card>
    </div>
  );
}

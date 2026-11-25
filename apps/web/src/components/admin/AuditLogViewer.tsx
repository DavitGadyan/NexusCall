"use client";
/**
 * Audit log viewer for the tenant (`/api/v1/audit/events`) and the platform (`/api/v1/platform/audit/events`).
 * Filters map 1:1 to the API; results use cursor pagination. Metadata holds ids and enums only.
 */
import { Download } from "lucide-react";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { downloadText } from "@/lib/client/download";
import { useCursorList } from "@/lib/client/use-api";
import { toCsv } from "@/lib/csv";
import { formatDateTime, shortId } from "@/lib/format";
import type { AuditEvent } from "@/lib/types";
import { Button, Card, type Column, DataTable, Dialog, EmptyState, Field, Input, KeyValues, Select, StatusBadge } from "@/components/ui";

type Filters = {
  action: string;
  actor_id: string;
  resource_type: string;
  resource_id: string;
  outcome: string;
  from: string;
  to: string;
  tenant_id: string;
};

const EMPTY: Filters = { action: "", actor_id: "", resource_type: "", resource_id: "", outcome: "", from: "", to: "", tenant_id: "" };

function toIso(local: string): string {
  if (!local) return "";
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString();
}

export function AuditLogViewer({ endpoint, platform = false }: { endpoint: string; platform?: boolean }) {
  const [draft, setDraft] = React.useState<Filters>(EMPTY);
  const [applied, setApplied] = React.useState<Filters>(EMPTY);
  const [selected, setSelected] = React.useState<AuditEvent | null>(null);

  const path = withQuery(endpoint, {
    action: applied.action.trim(),
    actor_id: applied.actor_id.trim(),
    resource_type: applied.resource_type.trim(),
    resource_id: applied.resource_id.trim(),
    outcome: applied.outcome,
    from: toIso(applied.from),
    to: toIso(applied.to),
    tenant_id: platform ? applied.tenant_id.trim() : undefined,
  });
  const list = useCursorList<AuditEvent>(path, 50);

  const set = (k: keyof Filters) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setDraft((d) => ({ ...d, [k]: e.target.value }));

  const columns: Column<AuditEvent>[] = [
    { key: "time", header: "Time", cell: (e) => <span className="whitespace-nowrap tabular">{formatDateTime(e.occurred_at)}</span> },
    { key: "action", header: "Action", cell: (e) => <span className="font-mono text-xs">{e.action}</span> },
    { key: "actor", header: "Actor", cell: (e) => <span className="font-mono text-xs" title={e.actor_id}>{`${e.actor_type}:${shortId(e.actor_id)}`}</span>, hideOnMobile: true },
    { key: "resource", header: "Resource", cell: (e) => <span className="font-mono text-xs" title={e.resource_id ?? ""}>{`${e.resource_type}${e.resource_id ? `:${shortId(e.resource_id)}` : ""}`}</span>, hideOnMobile: true },
    ...(platform ? [{ key: "tenant", header: "Tenant", cell: (e: AuditEvent) => <span className="font-mono text-xs">{shortId(e.tenant_id ?? null)}</span>, hideOnMobile: true }] : []),
    { key: "outcome", header: "Outcome", cell: (e) => <StatusBadge status={e.outcome} /> },
    {
      key: "details",
      header: <span className="sr-only">Details</span>,
      cell: (e) => (
        <Button size="sm" variant="ghost" onClick={() => setSelected(e)} aria-label={`Details for ${e.action} at ${formatDateTime(e.occurred_at)}`}>
          Details
        </Button>
      ),
    },
  ];

  function exportLoaded() {
    downloadText(
      `audit-${new Date().toISOString().slice(0, 19)}.csv`,
      toCsv([
        ["occurred_at", "tenant_id", "actor_type", "actor_id", "action", "resource_type", "resource_id", "outcome", "request_id", "producer"],
        ...list.items.map((e) => [e.occurred_at, e.tenant_id ?? "", e.actor_type, e.actor_id, e.action, e.resource_type, e.resource_id ?? "", e.outcome, e.request_id ?? "", e.producer]),
      ]),
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <form
          className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied(draft);
          }}
        >
          <Field id="f-action" label="Action">{(p) => <Input {...p} placeholder="e.g. invitation.created" value={draft.action} onChange={set("action")} />}</Field>
          <Field id="f-actor" label="Actor ID">{(p) => <Input {...p} value={draft.actor_id} onChange={set("actor_id")} />}</Field>
          <Field id="f-rtype" label="Resource type">{(p) => <Input {...p} placeholder="e.g. membership" value={draft.resource_type} onChange={set("resource_type")} />}</Field>
          <Field id="f-rid" label="Resource ID">{(p) => <Input {...p} value={draft.resource_id} onChange={set("resource_id")} />}</Field>
          <Field id="f-outcome" label="Outcome">
            {(p) => (
              <Select {...p} value={draft.outcome} onChange={set("outcome")}>
                <option value="">Any</option>
                <option value="success">Success</option>
                <option value="failure">Failure</option>
                <option value="denied">Denied</option>
              </Select>
            )}
          </Field>
          <Field id="f-from" label="From">{(p) => <Input {...p} type="datetime-local" value={draft.from} onChange={set("from")} />}</Field>
          <Field id="f-to" label="To">{(p) => <Input {...p} type="datetime-local" value={draft.to} onChange={set("to")} />}</Field>
          {platform ? <Field id="f-tenant" label="Tenant ID">{(p) => <Input {...p} value={draft.tenant_id} onChange={set("tenant_id")} />}</Field> : null}
          <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-4">
            <Button type="submit">Apply filters</Button>
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setDraft(EMPTY);
                setApplied(EMPTY);
              }}
            >
              Clear
            </Button>
            <Button type="button" variant="ghost" onClick={exportLoaded} disabled={list.items.length === 0} className="ml-auto">
              <Download className="h-4 w-4" aria-hidden /> Download loaded rows
            </Button>
          </div>
        </form>
      </Card>
      <Card>
        <DataTable
          caption="Audit events"
          columns={columns}
          rows={list.items}
          rowKey={(e) => e.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No audit events" description="Nothing matches these filters yet." />}
        />
      </Card>
      <Dialog open={selected !== null} onClose={() => setSelected(null)} title="Audit event" size="lg">
        {selected ? (
          <div className="flex flex-col gap-4">
            <KeyValues
              items={[
                { label: "Time", value: formatDateTime(selected.occurred_at) },
                { label: "Action", value: <span className="font-mono">{selected.action}</span> },
                { label: "Outcome", value: <StatusBadge status={selected.outcome} /> },
                { label: "Actor", value: <span className="font-mono">{`${selected.actor_type}:${selected.actor_id}`}</span> },
                { label: "Resource", value: <span className="font-mono">{`${selected.resource_type}:${selected.resource_id ?? "—"}`}</span> },
                ...(platform ? [{ label: "Tenant", value: <span className="font-mono">{selected.tenant_id ?? "platform"}</span> }] : []),
                { label: "Producer", value: selected.producer },
                { label: "Request ID", value: <span className="font-mono">{selected.request_id ?? "—"}</span> },
                { label: "Correlation ID", value: <span className="font-mono">{selected.correlation_id ?? "—"}</span> },
              ]}
            />
            <div>
              <p className="mb-1 text-sm font-medium">Metadata</p>
              <pre className="max-h-72 overflow-auto rounded-md bg-surface-2 p-3 font-mono text-xs">{JSON.stringify(selected.metadata ?? {}, null, 2)}</pre>
            </div>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}

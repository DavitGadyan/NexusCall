"use client";
/**
 * Analytics exports: an audited async job (needs `analytics:export` and a recent step-up). The download
 * link is requested only on click: the service issues a short-lived presigned URL (each one audited) that
 * the UI follows once and never stores.
 */
import { Download, Plus } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, FormError, Input, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { newIdempotencyKey, openDownload } from "@/components/crm/lib/http";
import { usePoll } from "@/components/crm/lib/use-poll";
import { StepUpHint } from "@/components/platform-ops/common";
import { type DateRange, formatUtcDay, presetRange, validateRange } from "./lib/dates";
import { metricLabel } from "./lib/format";
import { type AnalyticsExport, EXPORT_TERMINAL } from "./lib/types";

const TONE: Record<string, "neutral" | "ok" | "danger" | "brand"> = { queued: "neutral", processing: "brand", succeeded: "ok", failed: "danger" };

export function Exports() {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<AnalyticsExport>("/api/v1/exports");
  const [creating, setCreating] = React.useState(false);
  const [busy, setBusy] = React.useState<string | null>(null);
  usePoll(list.reload, list.items.some((e) => !EXPORT_TERMINAL.has(e.status)));

  async function download(e: AnalyticsExport) {
    setBusy(e.id);
    try {
      const fresh = await api<AnalyticsExport>(`/api/v1/exports/${e.id}`);
      if (!fresh.download_url || !openDownload(fresh.download_url)) {
        toast({ tone: "error", title: "The file is not available", description: fresh.status === "succeeded" ? "Try again in a moment." : `The export is ${fresh.status}.` });
      }
    } catch (err) {
      const x = toApiError(err);
      if (!x.isStepUp) toast({ tone: "error", title: "Could not get the download link", description: x.message });
    } finally {
      setBusy(null);
    }
  }

  const range = (e: AnalyticsExport) => {
    const from = typeof e.params.from === "string" ? e.params.from : "";
    const to = typeof e.params.to === "string" ? e.params.to : "";
    return from && to ? `${formatUtcDay(from, { year: true })} – ${formatUtcDay(to, { year: true })}` : "—";
  };

  const columns: Column<AnalyticsExport>[] = [
    { key: "id", header: "Export", cell: (e) => <span className="font-mono text-xs">{shortId(e.id)}</span> },
    { key: "range", header: "Range (UTC)", cell: range },
    {
      key: "status",
      header: "Status",
      cell: (e) => (
        <span className="flex flex-col gap-0.5">
          <Badge tone={TONE[e.status] ?? "neutral"}>{e.status}</Badge>
          {e.error_code ? <span className="text-xs text-danger">{e.error_code.replace(/_/g, " ")}</span> : null}
        </span>
      ),
    },
    { key: "rows", header: "Rows", hideOnMobile: true, cell: (e) => <span className="tabular">{e.row_count ?? "—"}</span> },
    { key: "created", header: "Requested", hideOnMobile: true, cell: (e) => <span title={formatDateTime(e.created_at)}>{relativeTime(e.created_at)}</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (e) =>
        e.status === "succeeded" ? (
          <Button size="sm" variant="secondary" onClick={() => download(e)} disabled={busy === e.id}>
            <Download className="h-4 w-4" aria-hidden /> {busy === e.id ? "Preparing…" : "Download"}
          </Button>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Exports"
        description="Daily metrics as CSV. Every export and every download link is recorded in the audit log; links expire quickly."
        actions={
          can("analytics:export") ? (
            <Button onClick={() => setCreating(true)} data-testid="new-export">
              <Plus className="h-4 w-4" aria-hidden /> New export
            </Button>
          ) : null
        }
      />
      <DataTable
        caption="Analytics exports"
        columns={columns}
        rows={list.items}
        rowKey={(e) => e.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No exports yet" description="Create an export to download daily metrics as CSV." />}
      />
      <CreateExportDialog open={creating} onClose={() => setCreating(false)} onCreated={list.reload} />
    </Card>
  );
}

function CreateExportDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const metrics = useApi<{ items: string[] }>(open ? "/api/v1/analytics/metrics" : null);
  const [range, setRange] = React.useState<DateRange>(() => presetRange(30));
  const [selected, setSelected] = React.useState<string[]>([]);
  const [dims, setDims] = React.useState(false);
  const [rangeError, setRangeError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef(newIdempotencyKey());

  React.useEffect(() => {
    if (!open) return;
    setRange(presetRange(30));
    setSelected([]);
    setDims(false);
    setRangeError(null);
    setError(null);
    key.current = newIdempotencyKey();
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const err = validateRange(range);
    setRangeError(err);
    if (err) return;
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/exports", {
        method: "POST",
        idempotencyKey: key.current,
        body: { kind: "daily_metrics", from: range.from, to: range.to, metrics: selected.length ? selected : null, include_dimensions: dims },
      });
      toast({ tone: "success", title: "Export requested", description: "It appears below when it is ready." });
      onCreated();
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      if (x.status !== 0 && !x.isStepUp) key.current = newIdempotencyKey();
    } finally {
      setBusy(false);
    }
  }

  const all = metrics.data?.items ?? [];
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="New analytics export" size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <StepUpHint />
        <div className="flex flex-wrap gap-3">
          <label className="flex flex-col gap-1 text-sm">
            From (UTC)
            <Input type="date" value={range.from} onChange={(e) => setRange((r) => ({ ...r, from: e.target.value }))} className="w-44" aria-invalid={rangeError ? true : undefined} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            To (UTC)
            <Input type="date" value={range.to} onChange={(e) => setRange((r) => ({ ...r, to: e.target.value }))} className="w-44" aria-invalid={rangeError ? true : undefined} />
          </label>
        </div>
        {rangeError || error?.fields.from || error?.fields.to ? (
          <p className="text-xs text-danger">{rangeError ?? error?.fields.from ?? error?.fields.to}</p>
        ) : null}
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Metrics</legend>
          <p className="text-xs text-muted">Leave all unchecked to export every metric.</p>
          <div className="grid max-h-56 grid-cols-1 gap-1.5 overflow-y-auto rounded-md border border-border p-3 sm:grid-cols-2">
            {all.map((m) => (
              <Checkbox
                key={m}
                label={metricLabel(m)}
                checked={selected.includes(m)}
                onChange={(e) => setSelected((s) => (e.target.checked ? [...s, m] : s.filter((x) => x !== m)))}
              />
            ))}
          </div>
        </fieldset>
        <Checkbox label="Include per-campaign and per-channel breakdowns" checked={dims} onChange={(e) => setDims(e.target.checked)} />
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} data-testid="create-export">
            {busy ? "Requesting…" : "Request export"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

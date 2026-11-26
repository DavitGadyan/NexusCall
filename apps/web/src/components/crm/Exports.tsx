"use client";
/**
 * Lead exports: async jobs with status. A download link is requested only when the user clicks Download:
 * the service issues a short-lived presigned URL (audited) that the UI follows once and never stores.
 */
import { Download } from "lucide-react";
import * as React from "react";
import { api, toApiError } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import { Button, Card, CardHeader, type Column, DataTable, EmptyState, useToast } from "@/components/ui";
import { JobBadge } from "./common";
import { openDownload } from "./lib/http";
import { type ExportJob, TERMINAL_JOB } from "./lib/types";
import { usePoll } from "./lib/use-poll";

export function Exports() {
  const toast = useToast();
  const list = useCursorList<ExportJob>("/api/v1/leads/exports");
  const [busy, setBusy] = React.useState<string | null>(null);
  usePoll(list.reload, list.items.some((e) => !TERMINAL_JOB.has(e.status)));

  async function download(e: ExportJob) {
    setBusy(e.id);
    try {
      const fresh = await api<ExportJob>(`/api/v1/leads/exports/${e.id}`);
      if (!fresh.download_url || !openDownload(fresh.download_url)) {
        toast({ tone: "error", title: "The file is not available", description: fresh.status === "completed" ? "Try again in a moment." : `The export is ${fresh.status}.` });
      }
    } catch (err) {
      const x = toApiError(err);
      if (!x.isStepUp) toast({ tone: "error", title: "Could not get the download link", description: x.message });
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<ExportJob>[] = [
    { key: "id", header: "Export", cell: (e) => <span className="font-mono text-xs">{shortId(e.id)}</span> },
    { key: "status", header: "Status", cell: (e) => <span className="flex flex-col gap-0.5"><JobBadge status={e.status} />{e.error_code ? <span className="text-xs text-danger">{e.error_code.replace(/_/g, " ")}</span> : null}</span> },
    { key: "rows", header: "Rows", cell: (e) => <span className="tabular">{e.row_count ?? "—"}</span> },
    { key: "created", header: "Requested", cell: (e) => <span title={formatDateTime(e.created_at)}>{relativeTime(e.created_at)}</span> },
    { key: "done", header: "Finished", cell: (e) => (e.completed_at ? relativeTime(e.completed_at) : "—"), hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (e) =>
        e.status === "completed" ? (
          <Button size="sm" variant="secondary" onClick={() => download(e)} disabled={busy === e.id}>
            <Download className="h-4 w-4" aria-hidden /> {busy === e.id ? "Preparing…" : "Download"}
          </Button>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardHeader title="Exports" description="Request exports from the Leads page. Links expire quickly; request a new one each time you download." />
      <DataTable
        caption="Exports"
        columns={columns}
        rows={list.items}
        rowKey={(e) => e.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No exports yet" description="Use Export on the Leads page." />}
      />
    </Card>
  );
}

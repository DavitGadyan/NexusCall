"use client";
/** Dual-approval queue for critical platform changes (build.md §20.2). Requesters cannot approve their own. */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime, shortId } from "@/lib/format";
import type { Page, PrivilegedRequest } from "@/lib/types";
import { Button, Card, CardHeader, type Column, DataTable, Dialog, EmptyState, Field, FormError, StatusBadge, Textarea, useToast } from "@/components/ui";
import { useCan, useSession } from "@/components/shell/session-context";

export function PrivilegedRequests() {
  const can = useCan();
  const session = useSession();
  const toast = useToast();
  const list = useApi<Page<PrivilegedRequest>>("/api/v1/platform/iam/privileged-requests");
  const [deciding, setDeciding] = React.useState<{ req: PrivilegedRequest; approve: boolean } | null>(null);
  const manage = can("platform:iam:manage");

  const columns: Column<PrivilegedRequest>[] = [
    { key: "kind", header: "Request", cell: (r) => <span className="font-medium">{humanize(r.kind)}</span> },
    {
      key: "target",
      header: "Target",
      cell: (r) => (
        <span className="text-sm">
          {r.payload.role_key ? <span className="font-mono text-xs">{String(r.payload.role_key)}</span> : null}
          {r.payload.user_id ? (
            <>
              {" for "}
              <Link className="text-brand hover:underline" href={`/platform/users/${String(r.payload.user_id)}`}>
                {shortId(String(r.payload.user_id))}
              </Link>
            </>
          ) : null}
        </span>
      ),
    },
    { key: "reason", header: "Reason", cell: (r) => <span className="line-clamp-2 max-w-sm">{r.reason}</span> },
    { key: "by", header: "Requested by", cell: (r) => <span className="font-mono text-xs">{r.requested_by === session.user.id ? "you" : shortId(r.requested_by)}</span>, hideOnMobile: true },
    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
    { key: "expires", header: "Expires", cell: (r) => <span title={formatDateTime(r.expires_at)}>{r.status === "pending" ? relativeTime(r.expires_at) : "—"}</span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Decision</span>,
      className: "text-right",
      cell: (r) => {
        if (!manage || r.status !== "pending") return null;
        if (r.requested_by === session.user.id) return <span className="text-xs text-muted">Needs another approver</span>;
        return (
          <div className="flex justify-end gap-1">
            <Button size="sm" onClick={() => setDeciding({ req: r, approve: true })}>
              Approve
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDeciding({ req: r, approve: false })}>
              Reject
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <Card>
      <CardHeader title="Privileged requests" description="Critical changes such as granting a platform role wait here until a second administrator decides." />
      <DataTable
        caption="Privileged requests"
        columns={columns}
        rows={list.data?.items ?? []}
        rowKey={(r) => r.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        empty={<EmptyState title="Nothing waiting" description="New platform-role requests appear here." />}
      />
      <DecisionDialog
        decision={deciding}
        onClose={() => setDeciding(null)}
        onDone={(status) => {
          setDeciding(null);
          list.reload();
          toast({ tone: "success", title: `Request ${status}` });
        }}
      />
    </Card>
  );
}

function DecisionDialog({
  decision,
  onClose,
  onDone,
}: {
  decision: { req: PrivilegedRequest; approve: boolean } | null;
  onClose: () => void;
  onDone: (status: string) => void;
}) {
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    setNote("");
    setError(null);
  }, [decision]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!decision) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ status: string }>(`/api/v1/platform/iam/privileged-requests/${decision.req.id}/decision`, {
        method: "POST",
        body: { approve: decision.approve, note: note.trim() },
      });
      onDone(out.status);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={decision !== null}
      onClose={onClose}
      busy={busy}
      title={decision?.approve ? "Approve request" : "Reject request"}
      description={decision?.approve ? "Approving applies the change immediately and is recorded with your identity." : "The request is closed without changes."}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {decision ? <p className="rounded-md bg-surface-2 px-3 py-2 text-sm">{decision.req.reason}</p> : null}
        <Field id="decision-note" label="Note (optional)">
          {(p) => <Textarea {...p} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />}
        </Field>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant={decision?.approve ? "primary" : "danger"} disabled={busy}>
            {busy ? "Saving…" : decision?.approve ? "Approve" : "Reject"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

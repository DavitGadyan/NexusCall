"use client";
/**
 * Suppression list for the workspace. Addresses are shown masked (the service never returns them in full).
 * Adding is immediate; removing needs step-up and a typed confirmation.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime } from "@/lib/format";
import { Button, Card, CardHeader, type Column, ConfirmDialog, DataTable, Dialog, EmptyState, Field, Input, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError, StepUpHint } from "./common";
import { FilterSelect } from "./Contacts";
import type { Suppression } from "./lib/types";

const REASONS = ["manual", "opt_out", "bounce", "complaint", "legal"];

export function Suppressions() {
  const can = useCan();
  const toast = useToast();
  const [channel, setChannel] = React.useState("");
  const list = useCursorList<Suppression>(withQuery("/api/v1/suppressions", { channel }));
  const [adding, setAdding] = React.useState(false);
  const [removing, setRemoving] = React.useState<Suppression | null>(null);
  const edit = can("leads:update");

  const columns: Column<Suppression>[] = [
    { key: "target", header: "Suppressed", cell: (s) => (s.contact_id ? <Link className="text-brand hover:underline" href={`/app/crm/contacts/${s.contact_id}`}>Contact</Link> : <span className="font-mono text-xs">{s.value_masked ?? "—"}</span>) },
    { key: "channel", header: "Channel", cell: (s) => humanize(s.channel) },
    { key: "reason", header: "Reason", cell: (s) => humanize(s.reason) },
    { key: "source", header: "Source", cell: (s) => humanize(s.source_type), hideOnMobile: true },
    { key: "at", header: "Since", cell: (s) => <span title={formatDateTime(s.created_at)}>{relativeTime(s.created_at)}</span> },
    { key: "actions", header: <span className="sr-only">Actions</span>, className: "text-right", cell: (s) => (edit ? <Button size="sm" variant="ghost" onClick={() => setRemoving(s)}>Unsuppress</Button> : null) },
  ];

  return (
    <Card>
      <CardHeader
        title="Suppressions"
        description="Opt-outs, bounces, complaints and manual blocks. They override consent on every channel they cover."
        actions={edit ? <Button size="sm" variant="danger" onClick={() => setAdding(true)}>Suppress an address</Button> : null}
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
        <FilterSelect id="supp-channel-filter" label="Channel" value={channel} onChange={setChannel} options={["phone", "email", "all"]} />
      </div>
      <DataTable caption="Suppressions" columns={columns} rows={list.items} rowKey={(s) => s.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="Nothing is suppressed" />} />
      <AddressDialog open={adding} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); list.reload(); toast({ tone: "success", title: "Address suppressed" }); }} />
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Remove suppression"
        description={<span className="flex flex-col gap-2"><span>Calls or emails may resume where consent allows. Make sure the original reason no longer applies.</span><span><StepUpHint /> You may be asked to verify with your authenticator.</span></span>}
        confirmLabel="Unsuppress"
        typedConfirmation="UNSUPPRESS"
        onConfirm={async () => {
          if (!removing) return;
          await api(`/api/v1/suppressions/${removing.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Suppression removed" });
        }}
      />
    </Card>
  );
}

function AddressDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const [channel, setChannel] = React.useState("email");
  const [value, setValue] = React.useState("");
  const [reason, setReason] = React.useState("manual");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setChannel("email");
      setValue("");
      setReason("manual");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/suppressions", { method: "POST", body: { channel, value: value.trim(), reason } });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Suppress an address" description="Blocks this email address or phone number even if no contact uses it yet.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="addr-channel" label="Channel" error={error?.fields.channel}>
          {(p) => <Select {...p} value={channel} onChange={(e) => setChannel(e.target.value)}><option value="email">Email</option><option value="phone">Phone</option></Select>}
        </Field>
        <Field id="addr-value" label={channel === "email" ? "Email address" : "Phone number"} required error={error?.fields.value}>
          {(p) => <Input {...p} type={channel === "email" ? "email" : "tel"} autoComplete="off" minLength={3} maxLength={320} value={value} onChange={(e) => setValue(e.target.value)} />}
        </Field>
        <Field id="addr-reason" label="Reason" error={error?.fields.reason}>
          {(p) => <Select {...p} value={reason} onChange={(e) => setReason(e.target.value)}>{REASONS.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}</Select>}
        </Field>
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" variant="danger" disabled={busy || value.trim().length < 3}>{busy ? "Saving…" : "Suppress"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

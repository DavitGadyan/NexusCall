"use client";
/** Lead detail and edit (status, score, qualification, source) with optimistic locking. */
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, humanize, shortId } from "@/lib/format";
import { Button, Card, CardHeader, ConfirmDialog, ErrorState, Field, Input, KeyValues, LoadingState, PageHeader, Select, StatusBadge, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "./common";
import { contactName, type Lead, LEAD_STATUSES } from "./lib/types";

export function LeadDetail({ leadId }: { leadId: string }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const lead = useApi<Lead>(`/api/v1/leads/${leadId}`);
  const [form, setForm] = React.useState({ status: "new", score: "", qualified: "", source: "" });
  const [error, setError] = React.useState<ApiError | null>(null);
  const [localError, setLocalError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);

  const l = lead.data;
  React.useEffect(() => {
    if (!l) return;
    setForm({ status: l.status, score: l.score === null ? "" : String(l.score), qualified: l.qualified === null ? "" : String(l.qualified), source: l.source ?? "" });
  }, [l]);

  if (lead.loading && !l) return <Card><LoadingState /></Card>;
  if (lead.error && !l) return <Card><ErrorState problem={lead.error.problem} onRetry={lead.reload} /></Card>;
  if (!l) return null;
  const edit = can("leads:update");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!l) return;
    setLocalError(null);
    setError(null);
    const s = form.score.trim();
    if (s && (!/^\d+$/.test(s) || Number(s) > 100)) return setLocalError("Score must be a whole number from 0 to 100.");
    const next = { status: form.status, score: s ? Number(s) : null, qualified: form.qualified === "" ? null : form.qualified === "true", source: form.source.trim() || null };
    const patch: Record<string, unknown> = {};
    if (next.status !== l.status) patch.status = next.status;
    if (next.score !== l.score) patch.score = next.score;
    if (next.qualified !== l.qualified) patch.qualified = next.qualified;
    if (next.source !== l.source) patch.source = next.source;
    if (Object.keys(patch).length === 0) return;
    setBusy(true);
    try {
      const saved = await api<Lead>(`/api/v1/leads/${l.id}`, { method: "PATCH", body: { ...patch, expected_version: l.version } });
      lead.setData(() => saved);
      toast({ tone: "success", title: "Lead saved" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={contactName(l.contact)}
        description={`Lead ${shortId(l.id)} · created ${formatDateTime(l.created_at)}`}
        actions={edit ? <Button variant="ghost" onClick={() => setDeleting(true)}>Delete lead</Button> : null}
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Lead" />
          {edit ? (
            <form onSubmit={save} className="flex flex-col gap-4 px-5 py-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field id="lead-status" label="Status" error={error?.fields.status}>
                  {(p) => <Select {...p} value={form.status} onChange={(e) => setForm((f) => ({ ...f, status: e.target.value }))}>{LEAD_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}</Select>}
                </Field>
                <Field id="lead-score" label="Score (0–100)" error={error?.fields.score}>
                  {(p) => <Input {...p} inputMode="numeric" value={form.score} onChange={(e) => setForm((f) => ({ ...f, score: e.target.value }))} />}
                </Field>
                <Field id="lead-qualified" label="Qualified" error={error?.fields.qualified}>
                  {(p) => (
                    <Select {...p} value={form.qualified} onChange={(e) => setForm((f) => ({ ...f, qualified: e.target.value }))}>
                      <option value="">Not decided</option>
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </Select>
                  )}
                </Field>
                <Field id="lead-source-edit" label="Source" error={error?.fields.source}>
                  {(p) => <Input {...p} maxLength={64} value={form.source} onChange={(e) => setForm((f) => ({ ...f, source: e.target.value }))} />}
                </Field>
              </div>
              {localError ? <p role="alert" className="text-sm text-danger">{localError}</p> : null}
              <MutationError error={error} onReload={() => { setError(null); lead.reload(); }} />
              <div className="flex justify-end">
                <Button type="submit" disabled={busy} data-testid="save-lead">{busy ? "Saving…" : "Save"}</Button>
              </div>
            </form>
          ) : (
            <div className="px-5 py-4">
              <KeyValues items={[
                { label: "Status", value: <StatusBadge status={l.status} /> },
                { label: "Score", value: l.score ?? "—" },
                { label: "Qualified", value: l.qualified === null ? "—" : l.qualified ? "Yes" : "No" },
                { label: "Source", value: l.source ?? "—" },
              ]} />
            </div>
          )}
        </Card>
        <Card>
          <CardHeader title="Links" />
          <div className="px-5 py-4">
            <KeyValues items={[
              { label: "Contact", value: <Link className="text-brand hover:underline" href={`/app/crm/contacts/${l.contact_id}`}>{contactName(l.contact)}</Link> },
              { label: "Campaign", value: l.campaign_id ? <Link className="text-brand hover:underline" href={`/app/campaigns/calls/${l.campaign_id}`}>{shortId(l.campaign_id)}</Link> : "—" },
              { label: "Last call", value: l.call_id ? <Link className="text-brand hover:underline" href={`/app/calls/${l.call_id}`}>{shortId(l.call_id)}</Link> : "—" },
              { label: "Booking", value: l.booking_id ? shortId(l.booking_id) : "—" },
              { label: "Last outcome", value: l.last_outcome ? humanize(l.last_outcome) : "—" },
              { label: "Updated", value: formatDateTime(l.updated_at) },
            ]} />
          </div>
        </Card>
      </div>
      <ConfirmDialog
        open={deleting}
        onClose={() => setDeleting(false)}
        title="Delete lead"
        description="The lead is removed. The contact, its consent and its call history stay."
        confirmLabel="Delete"
        onConfirm={async () => {
          await api(`/api/v1/leads/${l.id}`, { method: "DELETE" });
          toast({ tone: "success", title: "Lead deleted" });
          router.push("/app/crm/leads");
        }}
      />
    </>
  );
}

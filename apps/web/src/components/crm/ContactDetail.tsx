"use client";
/**
 * Contact detail: profile, custom fields, tags, contactability, consent history, suppression (unsuppress
 * needs step-up), notes, leads and an activity timeline. Archive is final and keeps consent evidence.
 */
import { X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime } from "@/lib/format";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  KeyValues,
  LoadingState,
  Notice,
  PageHeader,
  Select,
  StatusBadge,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { ContactDialog } from "./ContactDialog";
import { MutationError, StepUpHint } from "./common";
import { buildActivity, reasonLabel } from "./lib/activity";
import {
  type Consent,
  type Contact,
  type Contactability,
  contactName,
  type FieldDef,
  type Items,
  type Lead,
  type Note,
  type Suppression,
  type Tag,
} from "./lib/types";

export function ContactDetail({ contactId }: { contactId: string }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const base = `/api/v1/contacts/${contactId}`;
  const contact = useApi<Contact>(base);
  const fields = useApi<Items<FieldDef>>("/api/v1/custom-fields");
  const reach = useApi<Contactability>(`${base}/contactability`);
  const consents = useApi<Items<Consent>>(`${base}/consents`);
  const notes = useApi<Items<Note>>(`${base}/notes`);
  const supps = useApi<Items<Suppression>>(withQuery("/api/v1/suppressions", { contact_id: contactId, limit: 100 }));
  const leads = useApi<Items<Lead>>(withQuery("/api/v1/leads", { contact_id: contactId, limit: 50 }));
  const [tab, setTab] = React.useState("overview");
  const [editing, setEditing] = React.useState(false);
  const [archiving, setArchiving] = React.useState(false);

  if (contact.loading && !contact.data) return <Card><LoadingState /></Card>;
  if (contact.error && !contact.data) return <Card><ErrorState problem={contact.error.problem} onRetry={contact.reload} /></Card>;
  const c = contact.data;
  if (!c) return null;
  const update = can("leads:update") && c.status !== "archived";
  const refreshReach = () => {
    reach.reload();
    supps.reload();
    consents.reload();
  };

  return (
    <>
      <PageHeader
        title={contactName(c)}
        description={[c.company, c.email, c.phone_e164].filter(Boolean).join(" · ") || undefined}
        actions={
          update ? (
            <>
              <Button variant="secondary" onClick={() => setEditing(true)} data-testid="edit-contact">
                Edit
              </Button>
              <Button variant="ghost" onClick={() => setArchiving(true)}>
                Archive
              </Button>
            </>
          ) : null
        }
      />
      {c.status === "archived" ? <div className="mb-4"><Notice tone="warn" title="Archived">This contact can never be contacted again. Consent and suppression evidence is retained.</Notice></div> : null}
      <Tabs
        label="Contact sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "consent", label: "Consent", badge: consents.data ? <Badge>{consents.data.items.length}</Badge> : null },
          { id: "suppression", label: "Suppression", badge: supps.data?.items.length ? <Badge tone="danger">{supps.data.items.length}</Badge> : null },
          { id: "notes", label: "Notes", badge: notes.data ? <Badge>{notes.data.items.length}</Badge> : null },
          { id: "leads", label: "Leads", badge: leads.data ? <Badge>{leads.data.items.length}</Badge> : null },
          { id: "activity", label: "Activity" },
        ]}
      >
        {tab === "overview" ? (
          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Profile" />
              <div className="px-5 py-4">
                <KeyValues
                  items={[
                    { label: "Status", value: <StatusBadge status={c.status} /> },
                    { label: "Email", value: c.email ?? "—" },
                    { label: "Phone", value: c.phone_e164 ?? "—" },
                    { label: "Company", value: c.company ?? "—" },
                    { label: "Country", value: c.country ?? "—" },
                    { label: "Timezone", value: c.timezone ?? "—" },
                    { label: "Locale", value: c.locale ?? "—" },
                    { label: "Source", value: c.source ?? "—" },
                    { label: "Created", value: formatDateTime(c.created_at) },
                    { label: "Updated", value: formatDateTime(c.updated_at) },
                  ]}
                />
              </div>
              <CustomFieldValues contact={c} defs={fields.data?.items ?? []} />
            </Card>
            <div className="flex flex-col gap-4">
              <ReachCard reach={reach.data} loading={reach.loading} error={reach.error} onRetry={reach.reload} />
              <TagsCard contact={c} canEdit={update} onChanged={contact.reload} />
            </div>
          </div>
        ) : null}
        {tab === "consent" ? <ConsentTab contactId={contactId} items={consents.data?.items ?? []} loading={consents.loading} error={consents.error} onRetry={consents.reload} canEdit={update} onChanged={refreshReach} /> : null}
        {tab === "suppression" ? <SuppressionTab contact={c} items={supps.data?.items ?? []} loading={supps.loading} error={supps.error} onRetry={supps.reload} canEdit={can("leads:update")} onChanged={refreshReach} /> : null}
        {tab === "notes" ? <NotesTab contactId={contactId} items={notes.data?.items ?? []} loading={notes.loading} error={notes.error} onRetry={notes.reload} canEdit={update} onChanged={notes.reload} /> : null}
        {tab === "leads" ? <LeadsTab items={leads.data?.items ?? []} loading={leads.loading} error={leads.error} onRetry={leads.reload} /> : null}
        {tab === "activity" ? (
          <ActivityTab
            loading={consents.loading || notes.loading || leads.loading}
            items={buildActivity({ leads: leads.data?.items, consents: consents.data?.items, notes: notes.data?.items, suppressions: supps.data?.items })}
          />
        ) : null}
      </Tabs>
      <ContactDialog
        open={editing}
        onClose={() => setEditing(false)}
        contact={c}
        fields={fields.data?.items ?? []}
        onReload={contact.reload}
        onSaved={(saved) => {
          setEditing(false);
          contact.setData(() => saved);
          reach.reload();
          toast({ tone: "success", title: "Contact saved" });
        }}
      />
      <ConfirmDialog
        open={archiving}
        onClose={() => setArchiving(false)}
        title="Archive contact"
        description="An archived contact is never contacted again and disappears from lists. Consent and suppression evidence is kept. This cannot be undone here."
        confirmLabel="Archive"
        typedConfirmation="ARCHIVE"
        onConfirm={async () => {
          await api(base, { method: "DELETE" });
          toast({ tone: "success", title: "Contact archived" });
          router.push("/app/crm/contacts");
        }}
      />
    </>
  );
}

function CustomFieldValues({ contact, defs }: { contact: Contact; defs: FieldDef[] }) {
  const keys = new Set([...defs.map((d) => d.key), ...Object.keys(contact.custom_fields ?? {})]);
  if (keys.size === 0) return null;
  const label = (k: string) => defs.find((d) => d.key === k)?.label ?? `${k} (no longer defined)`;
  const show = (v: unknown) => (v === undefined || v === null || v === "" ? "—" : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v));
  return (
    <div className="border-t border-border px-5 py-4">
      <h3 className="mb-3 text-sm font-semibold">Custom fields</h3>
      <KeyValues items={[...keys].map((k) => ({ label: label(k), value: show(contact.custom_fields?.[k]) }))} />
    </div>
  );
}

function ReachCard({ reach, loading, error, onRetry }: { reach?: Contactability; loading: boolean; error?: ApiError; onRetry: () => void }) {
  return (
    <Card>
      <CardHeader title="Contactability" description="Checked on every call and email; this is the current state." />
      {loading && !reach ? <LoadingState rows={2} /> : error && !reach ? <ErrorState problem={error.problem} onRetry={onRetry} /> : reach ? (
        <ul className="flex flex-col gap-3 px-5 py-4 text-sm">
          {(["phone", "email"] as const).map((ch) => (
            <li key={ch} className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{ch === "phone" ? "Calls" : "Email"}</span>
              {reach[ch].allowed ? (
                <Badge tone="ok">Allowed</Badge>
              ) : (
                <span className="flex flex-wrap justify-end gap-1">
                  {reach[ch].reasons.map((r) => (
                    <Badge key={r} tone="danger">{reasonLabel(r)}</Badge>
                  ))}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

function TagsCard({ contact, canEdit, onChanged }: { contact: Contact; canEdit: boolean; onChanged: () => void }) {
  const toast = useToast();
  const all = useApi<Items<Tag>>(canEdit ? "/api/v1/tags" : null);
  const [pick, setPick] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const byName = new Map((all.data?.items ?? []).map((t) => [t.name, t]));
  const available = (all.data?.items ?? []).filter((t) => !contact.tags.includes(t.name));

  async function run(fn: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await fn();
      onChanged();
      toast({ tone: "success", title: done });
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Could not update tags", description: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Tags" />
      <div className="flex flex-col gap-3 px-5 py-4">
        {contact.tags.length === 0 ? <p className="text-sm text-muted">No tags.</p> : (
          <ul className="flex flex-wrap gap-1.5">
            {contact.tags.map((name) => {
              const tag = byName.get(name);
              return (
                <li key={name} className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 py-0.5 text-xs">
                  {tag?.color ? <span aria-hidden className="h-2 w-2 rounded-full" style={{ backgroundColor: tag.color }} /> : null}
                  {name}
                  {canEdit && tag ? (
                    <button type="button" aria-label={`Remove tag ${name}`} disabled={busy} className="rounded-full p-0.5 hover:bg-border" onClick={() => run(() => api(`/api/v1/contacts/${contact.id}/tags/${tag.id}`, { method: "DELETE" }), "Tag removed")}>
                      <X className="h-3 w-3" aria-hidden />
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {canEdit ? (
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (pick) run(() => api(`/api/v1/contacts/${contact.id}/tags`, { method: "POST", body: { tag_ids: [pick] } }), "Tag added").then(() => setPick(""));
            }}
          >
            <label htmlFor="add-tag" className="sr-only">Add a tag</label>
            <Select id="add-tag" value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">{available.length ? "Add a tag…" : "No more tags"}</option>
              {available.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </Select>
            <Button type="submit" size="sm" variant="secondary" disabled={!pick || busy}>Add</Button>
          </form>
        ) : null}
        {canEdit ? <Link href="/app/crm/fields?tab=tags" className="text-xs text-brand hover:underline">Manage tags</Link> : null}
      </div>
    </Card>
  );
}

type ListProps<T> = { items: T[]; loading: boolean; error?: ApiError; onRetry: () => void };

const LAWFUL_BASES = ["consent", "legitimate_interest", "contract", "legal_obligation", "vital_interest", "public_task"];

function ConsentTab({ contactId, canEdit, onChanged, ...list }: ListProps<Consent> & { contactId: string; canEdit: boolean; onChanged: () => void }) {
  const [open, setOpen] = React.useState(false);
  const columns: Column<Consent>[] = [
    { key: "at", header: "Captured", cell: (c) => <span title={formatDateTime(c.captured_at)}>{formatDateTime(c.captured_at)}</span> },
    { key: "channel", header: "Channel", cell: (c) => humanize(c.channel) },
    { key: "status", header: "Status", cell: (c) => <Badge tone={c.status === "granted" ? "ok" : "danger"}>{c.status}</Badge> },
    { key: "basis", header: "Lawful basis", cell: (c) => humanize(c.lawful_basis), hideOnMobile: true },
    { key: "source", header: "Source", cell: (c) => <span>{c.source}{c.source_ref ? <span className="block text-xs text-muted">{c.source_ref}</span> : null}</span> },
    { key: "policy", header: "Policy", cell: (c) => c.policy_version ?? "—", hideOnMobile: true },
    { key: "by", header: "Recorded by", cell: (c) => <span className="font-mono text-xs">{c.recorded_by}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader
        title="Consent history"
        description="Append-only. The latest record per channel decides; withdrawing consent stops contact on that channel."
        actions={canEdit ? <Button size="sm" onClick={() => setOpen(true)}>Record consent</Button> : null}
      />
      <DataTable caption="Consent history" columns={columns} rows={list.items} rowKey={(c) => c.id} loading={list.loading} error={list.error} onRetry={list.onRetry} empty={<EmptyState title="No consent records" />} />
      <ConsentDialog open={open} onClose={() => setOpen(false)} contactId={contactId} onSaved={() => { setOpen(false); onChanged(); }} />
    </Card>
  );
}

function ConsentDialog({ open, onClose, contactId, onSaved }: { open: boolean; onClose: () => void; contactId: string; onSaved: () => void }) {
  const toast = useToast();
  const [form, setForm] = React.useState({ channel: "phone", status: "granted", lawful_basis: "consent", source: "", source_ref: "", policy_version: "", evidence_ref: "" });
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setForm({ channel: "phone", status: "granted", lawful_basis: "consent", source: "", source_ref: "", policy_version: "", evidence_ref: "" });
      setError(null);
    }
  }, [open]);
  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/contacts/${contactId}/consents`, {
        method: "POST",
        body: {
          channel: form.channel,
          status: form.status,
          lawful_basis: form.lawful_basis,
          source: form.source.trim(),
          source_ref: form.source_ref.trim() || null,
          policy_version: form.policy_version.trim() || null,
          evidence_ref: form.evidence_ref.trim() || null,
        },
      });
      toast({ tone: "success", title: "Consent recorded" });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Record consent" description="Recorded with your identity and the current time as evidence." size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field id="consent-channel" label="Channel" error={error?.fields.channel}>
            {(p) => <Select {...p} value={form.channel} onChange={(e) => set("channel", e.target.value)}><option value="phone">Phone</option><option value="email">Email</option></Select>}
          </Field>
          <Field id="consent-status" label="Status" error={error?.fields.status}>
            {(p) => <Select {...p} value={form.status} onChange={(e) => set("status", e.target.value)}><option value="granted">Granted</option><option value="withdrawn">Withdrawn</option></Select>}
          </Field>
          <Field id="consent-basis" label="Lawful basis" error={error?.fields.lawful_basis}>
            {(p) => <Select {...p} value={form.lawful_basis} onChange={(e) => set("lawful_basis", e.target.value)}>{LAWFUL_BASES.map((b) => <option key={b} value={b}>{humanize(b)}</option>)}</Select>}
          </Field>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="consent-source" label="Source" required hint="Where it was captured, e.g. web_form or phone_call." error={error?.fields.source}>
            {(p) => <Input {...p} required maxLength={64} value={form.source} onChange={(e) => set("source", e.target.value)} />}
          </Field>
          <Field id="consent-source-ref" label="Source reference" error={error?.fields.source_ref}>
            {(p) => <Input {...p} maxLength={128} value={form.source_ref} onChange={(e) => set("source_ref", e.target.value)} />}
          </Field>
          <Field id="consent-policy" label="Policy version" error={error?.fields.policy_version}>
            {(p) => <Input {...p} maxLength={32} value={form.policy_version} onChange={(e) => set("policy_version", e.target.value)} />}
          </Field>
          <Field id="consent-evidence" label="Evidence reference" error={error?.fields.evidence_ref}>
            {(p) => <Input {...p} maxLength={255} value={form.evidence_ref} onChange={(e) => set("evidence_ref", e.target.value)} />}
          </Field>
        </div>
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || !form.source.trim()}>{busy ? "Saving…" : "Record"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

const SUPPRESSION_REASONS = ["manual", "opt_out", "bounce", "complaint", "legal"];

function SuppressionTab({ contact, canEdit, onChanged, ...list }: ListProps<Suppression> & { contact: Contact; canEdit: boolean; onChanged: () => void }) {
  const toast = useToast();
  const [adding, setAdding] = React.useState(false);
  const [removing, setRemoving] = React.useState<Suppression | null>(null);
  const columns: Column<Suppression>[] = [
    { key: "channel", header: "Channel", cell: (s) => humanize(s.channel) },
    { key: "reason", header: "Reason", cell: (s) => humanize(s.reason) },
    { key: "source", header: "Source", cell: (s) => humanize(s.source_type), hideOnMobile: true },
    { key: "value", header: "Address", cell: (s) => <span className="font-mono text-xs">{s.value_masked ?? "This contact"}</span>, hideOnMobile: true },
    { key: "at", header: "Since", cell: (s) => <span title={formatDateTime(s.created_at)}>{relativeTime(s.created_at)}</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (s) => (canEdit ? <Button size="sm" variant="ghost" onClick={() => setRemoving(s)}>Unsuppress</Button> : null),
    },
  ];
  return (
    <Card>
      <CardHeader
        title="Suppression"
        description="Suppressed contacts are never called or emailed on that channel, whatever their consent."
        actions={canEdit && contact.status !== "archived" ? <Button size="sm" variant="danger" onClick={() => setAdding(true)} data-testid="suppress">Suppress</Button> : null}
      />
      <DataTable caption="Suppressions" columns={columns} rows={list.items} rowKey={(s) => s.id} loading={list.loading} error={list.error} onRetry={list.onRetry} empty={<EmptyState title="Not suppressed" description="This contact can be reached on channels with consent." />} />
      <SuppressDialog open={adding} onClose={() => setAdding(false)} contactId={contact.id} onSaved={() => { setAdding(false); onChanged(); toast({ tone: "success", title: "Contact suppressed" }); }} />
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Remove suppression"
        tone="danger"
        description={
          <span className="flex flex-col gap-2">
            <span>The contact may be called or emailed again on {removing ? humanize(removing.channel).toLowerCase() : ""} when consent allows. Make sure the reason for the suppression no longer applies.</span>
            <span><StepUpHint /> You may be asked to verify with your authenticator.</span>
          </span>
        }
        confirmLabel="Unsuppress"
        typedConfirmation="UNSUPPRESS"
        onConfirm={async () => {
          if (!removing) return;
          await api(`/api/v1/suppressions/${removing.id}`, { method: "DELETE" });
          onChanged();
          toast({ tone: "success", title: "Suppression removed" });
        }}
      />
    </Card>
  );
}

function SuppressDialog({ open, onClose, contactId, onSaved }: { open: boolean; onClose: () => void; contactId: string; onSaved: () => void }) {
  const [channel, setChannel] = React.useState("all");
  const [reason, setReason] = React.useState("manual");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setChannel("all");
      setReason("manual");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/suppressions", { method: "POST", body: { contact_id: contactId, channel, reason } });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Suppress contact" description="Takes effect immediately for new calls and emails. Removing it later needs a recent MFA sign-in.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="supp-channel" label="Channel" error={error?.fields.channel}>
          {(p) => <Select {...p} value={channel} onChange={(e) => setChannel(e.target.value)}><option value="all">All channels</option><option value="phone">Phone</option><option value="email">Email</option></Select>}
        </Field>
        <Field id="supp-reason" label="Reason" error={error?.fields.reason}>
          {(p) => <Select {...p} value={reason} onChange={(e) => setReason(e.target.value)}>{SUPPRESSION_REASONS.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}</Select>}
        </Field>
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" variant="danger" disabled={busy} data-testid="confirm-suppress">{busy ? "Saving…" : "Suppress"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function NotesTab({ contactId, canEdit, onChanged, ...list }: ListProps<Note> & { contactId: string; canEdit: boolean; onChanged: () => void }) {
  const [text, setText] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/contacts/${contactId}/notes`, { method: "POST", body: { text: text.trim() } });
      setText("");
      onChanged();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader title="Notes" description="Notes are plain text and cannot be edited after saving." />
      {canEdit ? (
        <form onSubmit={submit} className="flex flex-col gap-2 border-b border-border px-5 py-4">
          <Field id="note-text" label="Add a note" error={error?.fields.text}>
            {(p) => <Textarea {...p} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <MutationError error={error} />
          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={busy || !text.trim()}>{busy ? "Saving…" : "Add note"}</Button>
          </div>
        </form>
      ) : null}
      {list.loading && list.items.length === 0 ? <LoadingState /> : list.error && list.items.length === 0 ? <ErrorState problem={list.error.problem} onRetry={list.onRetry} /> : list.items.length === 0 ? <EmptyState title="No notes yet" /> : (
        <ul className="divide-y divide-border">
          {list.items.map((n) => (
            <li key={n.id} className="px-5 py-3 text-sm">
              <p className="whitespace-pre-wrap break-words">{n.text}</p>
              <p className="mt-1 text-xs text-muted">
                {humanize(n.source_type)} · <span title={formatDateTime(n.created_at)}>{relativeTime(n.created_at)}</span> · <span className="font-mono">{n.created_by}</span>
              </p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function LeadsTab(list: ListProps<Lead>) {
  const columns: Column<Lead>[] = [
    { key: "status", header: "Status", cell: (l) => <Link className="text-brand hover:underline" href={`/app/crm/leads/${l.id}`}><StatusBadge status={l.status} /></Link> },
    { key: "score", header: "Score", cell: (l) => <span className="tabular">{l.score ?? "—"}</span> },
    { key: "source", header: "Source", cell: (l) => l.source ?? "—" },
    { key: "outcome", header: "Last outcome", cell: (l) => (l.last_outcome ? humanize(l.last_outcome) : "—"), hideOnMobile: true },
    { key: "updated", header: "Updated", cell: (l) => relativeTime(l.updated_at) },
    { key: "open", header: <span className="sr-only">Open</span>, className: "text-right", cell: (l) => <ButtonLink size="sm" href={`/app/crm/leads/${l.id}`}>Open</ButtonLink> },
  ];
  return (
    <Card>
      <CardHeader title="Leads" />
      <DataTable caption="Leads of this contact" columns={columns} rows={list.items} rowKey={(l) => l.id} loading={list.loading} error={list.error} onRetry={list.onRetry} empty={<EmptyState title="No leads for this contact" />} />
    </Card>
  );
}

function ActivityTab({ items, loading }: { items: ReturnType<typeof buildActivity>; loading: boolean }) {
  return (
    <Card>
      <CardHeader title="Activity" description="Lead changes, consent, notes and suppressions, newest first." />
      {loading && items.length === 0 ? <LoadingState /> : items.length === 0 ? <EmptyState title="No activity yet" /> : (
        <ol className="flex flex-col gap-0 px-5 py-3">
          {items.map((a) => (
            <li key={a.id} className="flex gap-3 border-l border-border py-2 pl-4 text-sm">
              <div className="min-w-0 flex-1">
                <p className="font-medium">{a.title}</p>
                {a.detail ? <p className="line-clamp-2 whitespace-pre-wrap break-words text-muted">{a.detail}</p> : null}
              </div>
              <time dateTime={a.at} title={formatDateTime(a.at)} className="shrink-0 text-xs text-muted">{relativeTime(a.at)}</time>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

"use client";
/** Leads: filters, sort, pagination, create (existing or new contact) and audited export requests. */
import { Download } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime } from "@/lib/format";
import { Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, Field, Input, Notice, Select, StatusBadge, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError, StepUpHint } from "./common";
import { FilterSelect, SortControl } from "./Contacts";
import { newIdempotencyKey } from "./lib/http";
import { type Contact, contactName, type ExportJob, type Lead, LEAD_STATUSES } from "./lib/types";

const SORTS = [
  { id: "created_at", label: "Created" },
  { id: "updated_at", label: "Updated" },
  { id: "score", label: "Score" },
] as const;

export function Leads() {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const [status, setStatus] = React.useState("");
  const [qualified, setQualified] = React.useState("");
  const [source, setSource] = React.useState("");
  const [sort, setSort] = React.useState("created_at");
  const [direction, setDirection] = React.useState<"asc" | "desc">("desc");
  const [creating, setCreating] = React.useState(false);
  const [exporting, setExporting] = React.useState(false);
  const [dsource, setDsource] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setDsource(source.trim()), 300);
    return () => clearTimeout(t);
  }, [source]);
  const list = useCursorList<Lead>(withQuery("/api/v1/leads", { status, qualified, source: dsource, sort, direction }));
  const filtered = Boolean(status || qualified || dsource);

  const columns: Column<Lead>[] = [
    {
      key: "contact",
      header: "Lead",
      cell: (l) => (
        <Link href={`/app/crm/leads/${l.id}`} className="block min-w-0 hover:underline">
          <span className="block truncate font-medium text-brand">{contactName(l.contact)}</span>
          <span className="block truncate text-xs text-muted">{l.contact?.email ?? l.contact?.phone_e164 ?? ""}</span>
        </Link>
      ),
    },
    { key: "status", header: "Status", cell: (l) => <StatusBadge status={l.status} /> },
    { key: "score", header: "Score", cell: (l) => <span className="tabular">{l.score ?? "—"}</span> },
    { key: "qualified", header: "Qualified", cell: (l) => (l.qualified === null ? "—" : l.qualified ? "Yes" : "No"), hideOnMobile: true },
    { key: "source", header: "Source", cell: (l) => l.source ?? "—", hideOnMobile: true },
    { key: "outcome", header: "Last outcome", cell: (l) => (l.last_outcome ? humanize(l.last_outcome) : "—"), hideOnMobile: true },
    { key: "created", header: "Created", cell: (l) => <span title={formatDateTime(l.created_at)}>{relativeTime(l.created_at)}</span>, hideOnMobile: true },
  ];

  return (
    <Card>
      <CardHeader
        title="Leads"
        description="Sales opportunities linked to a contact. Call outcomes and bookings update them automatically."
        actions={
          <div className="flex gap-2">
            {can("leads:export") ? (
              <Button size="sm" variant="secondary" onClick={() => setExporting(true)} data-testid="export-leads">
                <Download className="h-4 w-4" aria-hidden /> Export
              </Button>
            ) : null}
            {can("leads:create") ? (
              <Button size="sm" onClick={() => setCreating(true)} data-testid="new-lead">
                New lead
              </Button>
            ) : null}
          </div>
        }
      />
      <div className="flex flex-wrap items-end gap-2 border-b border-border px-4 py-3">
        <FilterSelect id="lead-status" label="Status" value={status} onChange={setStatus} options={LEAD_STATUSES} />
        <FilterSelect id="lead-qualified" label="Qualified" value={qualified} onChange={setQualified} options={[{ value: "true", label: "Yes" }, { value: "false", label: "No" }]} allLabel="Any" />
        <div className="w-36">
          <label htmlFor="lead-source" className="sr-only">Source</label>
          <Input id="lead-source" placeholder="Source" maxLength={64} value={source} onChange={(e) => setSource(e.target.value)} />
        </div>
        <SortControl sort={sort} direction={direction} onSort={setSort} onDirection={setDirection} options={SORTS} idPrefix="lead" />
      </div>
      <DataTable
        caption="Leads"
        columns={columns}
        rows={list.items}
        rowKey={(l) => l.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={filtered ? "No matching leads" : "No leads yet"} description={filtered ? "Try other filters." : "Create a lead, import contacts, or run a campaign."} />}
      />
      <LeadCreateDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(l) => {
          setCreating(false);
          toast({ tone: "success", title: "Lead created" });
          router.push(`/app/crm/leads/${l.id}`);
        }}
      />
      <ExportDialog
        open={exporting}
        onClose={() => setExporting(false)}
        initialStatus={status}
        onRequested={() => {
          setExporting(false);
          toast({ tone: "success", title: "Export requested", description: "It runs in the background. Download it from Exports when it is ready." });
          router.push("/app/crm/exports");
        }}
      />
    </Card>
  );
}

function LeadCreateDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (l: Lead) => void }) {
  const [mode, setMode] = React.useState<"existing" | "new">("existing");
  const [q, setQ] = React.useState("");
  const [contactId, setContactId] = React.useState("");
  const [person, setPerson] = React.useState({ first_name: "", last_name: "", email: "", phone: "", company: "" });
  const [status, setStatus] = React.useState("new");
  const [score, setScore] = React.useState("");
  const [source, setSource] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [localError, setLocalError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  const [dq, setDq] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setDq(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);
  const matches = useApi<{ items: Contact[] }>(open && mode === "existing" && dq ? withQuery("/api/v1/contacts", { q: dq, limit: 10 }) : null);

  React.useEffect(() => {
    if (!open) return;
    setMode("existing");
    setQ("");
    setContactId("");
    setPerson({ first_name: "", last_name: "", email: "", phone: "", company: "" });
    setStatus("new");
    setScore("");
    setSource("");
    setError(null);
    setLocalError(null);
    key.current = newIdempotencyKey();
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLocalError(null);
    setError(null);
    const s = score.trim();
    if (s && (!/^\d+$/.test(s) || Number(s) > 100)) return setLocalError("Score must be a whole number from 0 to 100.");
    const body: Record<string, unknown> = { status, score: s ? Number(s) : null, source: source.trim() || null };
    if (mode === "existing") {
      if (!contactId) return setLocalError("Choose a contact.");
      body.contact_id = contactId;
    } else {
      const c = Object.fromEntries(Object.entries(person).map(([k, v]) => [k, v.trim() || null]));
      if (!c.first_name && !c.last_name && !c.email && !c.phone && !c.company) return setLocalError("Enter a name, company, email or phone.");
      body.contact = c;
    }
    setBusy(true);
    try {
      onCreated(await api<Lead>("/api/v1/leads", { method: "POST", body, idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="New lead" size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <fieldset className="flex gap-4 text-sm">
          <legend className="sr-only">Contact</legend>
          <label className="inline-flex items-center gap-2"><input type="radio" name="lead-mode" checked={mode === "existing"} onChange={() => setMode("existing")} /> Existing contact</label>
          <label className="inline-flex items-center gap-2"><input type="radio" name="lead-mode" checked={mode === "new"} onChange={() => setMode("new")} /> New contact</label>
        </fieldset>
        {mode === "existing" ? (
          <div className="flex flex-col gap-2">
            <Field id="lead-contact-q" label="Find a contact">
              {(p) => <Input {...p} type="search" maxLength={100} placeholder="Name, company, email or phone" value={q} onChange={(e) => setQ(e.target.value)} />}
            </Field>
            {dq ? (
              <Field id="lead-contact" label="Contact" error={error?.fields.contact_id}>
                {(p) => (
                  <Select {...p} value={contactId} onChange={(e) => setContactId(e.target.value)}>
                    <option value="">{matches.loading ? "Searching…" : (matches.data?.items.length ?? 0) === 0 ? "No matches" : "Choose…"}</option>
                    {(matches.data?.items ?? []).map((c) => (
                      <option key={c.id} value={c.id}>{contactName(c)}{c.email ? ` · ${c.email}` : ""}</option>
                    ))}
                  </Select>
                )}
              </Field>
            ) : null}
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {(["first_name", "last_name", "company", "email", "phone"] as const).map((k) => (
              <Field key={k} id={`lead-new-${k}`} label={humanize(k)} error={error?.fields[`contact.${k}`]}>
                {(p) => <Input {...p} type={k === "email" ? "email" : k === "phone" ? "tel" : "text"} value={person[k]} onChange={(e) => setPerson((x) => ({ ...x, [k]: e.target.value }))} />}
              </Field>
            ))}
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <Field id="lead-new-status" label="Status" error={error?.fields.status}>
            {(p) => <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>{LEAD_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}</Select>}
          </Field>
          <Field id="lead-new-score" label="Score (0–100)" error={error?.fields.score}>
            {(p) => <Input {...p} inputMode="numeric" value={score} onChange={(e) => setScore(e.target.value)} />}
          </Field>
          <Field id="lead-new-source" label="Source" error={error?.fields.source}>
            {(p) => <Input {...p} maxLength={64} value={source} onChange={(e) => setSource(e.target.value)} />}
          </Field>
        </div>
        {localError ? <p role="alert" className="text-sm text-danger">{localError}</p> : null}
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy} data-testid="create-lead">{busy ? "Creating…" : "Create lead"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function ExportDialog({ open, onClose, onRequested, initialStatus }: { open: boolean; onClose: () => void; onRequested: (e: ExportJob) => void; initialStatus: string }) {
  const [statuses, setStatuses] = React.useState<Set<string>>(new Set());
  const [source, setSource] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [to, setTo] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (!open) return;
    setStatuses(new Set(initialStatus ? [initialStatus] : []));
    setSource("");
    setFrom("");
    setTo("");
    setError(null);
    key.current = newIdempotencyKey();
  }, [open, initialStatus]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const filters: Record<string, unknown> = {};
    if (statuses.size) filters.status = [...statuses];
    if (source.trim()) filters.source = source.trim();
    if (from) filters.created_from = new Date(`${from}T00:00:00`).toISOString();
    if (to) filters.created_to = new Date(`${to}T23:59:59`).toISOString();
    try {
      // Same key on retry (e.g. after verifying with MFA in this dialog session): one export, not two.
      onRequested(await api<ExportJob>("/api/v1/leads/exports", { method: "POST", body: { kind: "leads", filters }, idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Export leads" size="lg" description="Exports contain personal data. Each request and each download link is recorded in the audit log.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Notice tone="info"><span className="inline-flex flex-wrap items-center gap-2"><StepUpHint /> Exporting needs a recent multi-factor sign-in.</span></Notice>
        <fieldset>
          <legend className="mb-2 text-sm font-medium">Statuses (none = all)</legend>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {LEAD_STATUSES.map((s) => (
              <Checkbox key={s} label={humanize(s)} checked={statuses.has(s)} onChange={(e) => {
                const next = new Set(statuses);
                if (e.target.checked) next.add(s);
                else next.delete(s);
                setStatuses(next);
              }} />
            ))}
          </div>
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field id="export-source" label="Source" error={error?.fields["filters.source"]}>
            {(p) => <Input {...p} maxLength={64} value={source} onChange={(e) => setSource(e.target.value)} />}
          </Field>
          <Field id="export-from" label="Created from" error={error?.fields["filters.created_from"]}>
            {(p) => <Input {...p} type="date" value={from} onChange={(e) => setFrom(e.target.value)} />}
          </Field>
          <Field id="export-to" label="Created to" error={error?.fields["filters.created_to"]}>
            {(p) => <Input {...p} type="date" value={to} onChange={(e) => setTo(e.target.value)} />}
          </Field>
        </div>
        {from && to && from > to ? <p role="alert" className="text-sm text-danger">The start date is after the end date.</p> : null}
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || Boolean(from && to && from > to)} data-testid="request-export">{busy ? "Requesting…" : "Request export"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

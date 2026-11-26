"use client";
/** Knowledge: sources with a data classification, and a retrieval test that shows the chunks an agent would get. */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, Field, Input, Notice, Select, StatusBadge, Tabs, Textarea, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "@/components/crm/common";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import { CLASSIFICATIONS, type KnowledgeSource, type SearchOut } from "../lib/types";

export const CLASS_TONE: Record<string, "neutral" | "ok" | "warn" | "danger" | "brand"> = { public: "ok", internal: "neutral", confidential: "warn", restricted: "danger" };

export function ClassificationBadge({ value }: { value: string }) {
  return <Badge tone={CLASS_TONE[value] ?? "neutral"}>{value}</Badge>;
}

/** Mirrors ai_agent_service `source_requires_approval`: external or confidential+ sources always need review. */
export function approvalRequired(origin: string, classification: string, requested: boolean): boolean {
  return requested || origin === "external" || classification === "confidential" || classification === "restricted";
}

export function Knowledge() {
  const [tab, setTab] = React.useState("sources");
  return (
    <Tabs label="Knowledge" value={tab} onChange={setTab} tabs={[{ id: "sources", label: "Sources" }, { id: "search", label: "Search test" }]}>
      {tab === "sources" ? <Sources /> : <SearchTest />}
    </Tabs>
  );
}

function Sources() {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<KnowledgeSource>("/api/v1/ai/knowledge/sources");
  const [open, setOpen] = React.useState(false);
  const columns: Column<KnowledgeSource>[] = [
    { key: "name", header: "Source", cell: (s) => <Link className="font-medium text-brand hover:underline" href={`/app/knowledge/sources/${s.id}`}>{s.name}</Link> },
    { key: "class", header: "Classification", cell: (s) => <ClassificationBadge value={s.classification} /> },
    { key: "origin", header: "Origin", cell: (s) => s.origin, hideOnMobile: true },
    { key: "review", header: "Review", cell: (s) => (s.requires_approval ? <Badge tone="warn">Every document</Badge> : <span className="text-xs text-muted">Flagged only</span>), hideOnMobile: true },
    { key: "roles", header: "Restricted to", cell: (s) => (s.allowed_roles.length ? s.allowed_roles.join(", ") : "All roles"), hideOnMobile: true },
    { key: "status", header: "Status", cell: (s) => <StatusBadge status={s.status} /> },
    { key: "updated", header: "Updated", cell: (s) => <span title={formatDateTime(s.updated_at)}>{relativeTime(s.updated_at)}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader title="Knowledge sources" description="Documents agents may retrieve from. A source's classification caps where its content may be used." actions={can("ai:agents:manage") ? <Button size="sm" onClick={() => setOpen(true)} data-testid="new-source">New source</Button> : null} />
      <DataTable caption="Knowledge sources" columns={columns} rows={list.items} rowKey={(s) => s.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No knowledge sources" description="Create a source, then upload text, Markdown or PDF documents." />} />
      <SourceDialog open={open} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); list.reload(); toast({ tone: "success", title: "Source created" }); }} />
    </Card>
  );
}

function SourceDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: (s: KnowledgeSource) => void }) {
  const [f, setF] = React.useState({ name: "", description: "", classification: "internal", origin: "internal", requires_approval: false, roles: "" });
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (open) {
      setF({ name: "", description: "", classification: "internal", origin: "internal", requires_approval: false, roles: "" });
      setError(null);
      key.current = newIdempotencyKey();
    }
  }, [open]);
  const forced = approvalRequired(f.origin, f.classification, false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSaved(
        await api<KnowledgeSource>("/api/v1/ai/knowledge/sources", {
          method: "POST",
          idempotencyKey: key.current,
          body: {
            name: f.name.trim(),
            description: f.description.trim(),
            classification: f.classification,
            origin: f.origin,
            requires_approval: f.requires_approval || forced,
            allowed_roles: f.roles.split(",").map((r) => r.trim()).filter(Boolean),
          },
        }),
      );
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="lg" title="New knowledge source">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="src-name" label="Name" required error={error?.fields.name}>
            {(p) => <Input {...p} minLength={2} maxLength={120} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
          </Field>
          <Field id="src-class" label="Classification" error={error?.fields.classification}>
            {(p) => <Select {...p} value={f.classification} onChange={(e) => setF({ ...f, classification: e.target.value })}>{CLASSIFICATIONS.map((c) => <option key={c} value={c}>{c}</option>)}</Select>}
          </Field>
          <Field id="src-origin" label="Origin" hint="External content (supplier docs, web pages) is always reviewed." error={error?.fields.origin}>
            {(p) => <Select {...p} value={f.origin} onChange={(e) => setF({ ...f, origin: e.target.value })}><option value="internal">Internal</option><option value="external">External</option></Select>}
          </Field>
          <Field id="src-roles" label="Restrict to roles (optional)" hint="Comma-separated role keys." error={error?.fields.allowed_roles}>
            {(p) => <Input {...p} value={f.roles} onChange={(e) => setF({ ...f, roles: e.target.value })} />}
          </Field>
        </div>
        <Field id="src-desc" label="Description" error={error?.fields.description}>
          {(p) => <Textarea {...p} maxLength={500} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />}
        </Field>
        <Checkbox label="Every document needs human approval before agents can use it" checked={f.requires_approval || forced} disabled={forced} description={forced ? "Required for external, confidential and restricted sources." : undefined} onChange={(e) => setF({ ...f, requires_approval: e.target.checked })} />
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || f.name.trim().length < 2}>{busy ? "Creating…" : "Create source"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function SearchTest() {
  const can = useCan();
  const sources = useCursorList<KnowledgeSource>("/api/v1/ai/knowledge/sources", 200);
  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState("4");
  const [picked, setPicked] = React.useState<string[]>([]);
  const [result, setResult] = React.useState<SearchOut | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const scores = can("ai:agents:manage");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api<SearchOut>("/api/v1/ai/knowledge/search", { method: "POST", body: { query: query.trim(), top_k: Number(topK), source_ids: picked.length ? picked : null } }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="h-fit">
        <CardHeader title="Search test" description="Runs the same retrieval an agent would, with your roles and the workspace classification cap. Rate-limited." />
        <form onSubmit={submit} className="flex flex-col gap-4 px-5 py-4">
          <Field id="kq" label="Question" required error={error?.fields.query}>
            {(p) => <Textarea {...p} rows={3} maxLength={2000} value={query} onChange={(e) => setQuery(e.target.value)} />}
          </Field>
          <Field id="ktopk" label="Results">
            {(p) => <Select {...p} value={topK} onChange={(e) => setTopK(e.target.value)}>{[1, 2, 3, 4, 5, 6, 7, 8].map((n) => <option key={n} value={n}>{n}</option>)}</Select>}
          </Field>
          {sources.items.length ? (
            <fieldset>
              <legend className="mb-1 text-sm font-medium">Sources (none = all)</legend>
              <div className="flex max-h-48 flex-col gap-1.5 overflow-auto">
                {sources.items.map((s) => <Checkbox key={s.id} label={s.name} checked={picked.includes(s.id)} onChange={(e) => setPicked(e.target.checked ? [...picked, s.id] : picked.filter((x) => x !== s.id))} />)}
              </div>
            </fieldset>
          ) : null}
          {error?.status === 429 ? <Notice tone="warn">Too many searches. Wait a moment and try again.</Notice> : <MutationError error={error} />}
          <Button type="submit" disabled={busy || !query.trim()} data-testid="run-search">{busy ? "Searching…" : "Search"}</Button>
        </form>
      </Card>
      <Card className="lg:col-span-2">
        <CardHeader title="Chunks" description={result ? `Embedding model ${result.embedding_model}.${scores ? "" : " Scores are shown to agent managers only; results are in rank order."}` : undefined} />
        {!result ? <EmptyState title="Ask a question to see what agents would retrieve" /> : result.results.length === 0 ? <EmptyState title="Nothing retrieved" description="No approved, indexed chunk matched within your access and the classification cap." /> : (
          <ol className="divide-y divide-border">
            {result.results.map((r) => (
              <li key={r.chunk_id} className="flex flex-col gap-1 px-5 py-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                  <Badge tone="brand">#{r.rank}</Badge>
                  <Link className="text-brand hover:underline" href={`/app/knowledge/sources/${r.source_id}`}>{r.source_name}</Link>
                  {typeof r.similarity === "number" ? <span className="tabular">similarity {r.similarity.toFixed(3)}</span> : null}
                </div>
                {/* Retrieved text is untrusted content: rendered as a plain text node, never as HTML. */}
                <p className="whitespace-pre-wrap break-words text-sm">{r.text}</p>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}

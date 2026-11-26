"use client";
/** AI agents list and creation (the first prompt version is created with the agent, inactive until activated). */
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, Dialog, EmptyState, Field, Input, Select, StatusBadge, Textarea, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "@/components/crm/common";
import { FilterSelect } from "@/components/crm/Contacts";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import type { Items } from "@/components/crm/lib/types";
import { buildVersion, emptyVersionForm, mergeErrors, type VersionForm } from "./lib/version-form";
import type { Agent, KnowledgeSource } from "./lib/types";
import { VersionFields } from "./VersionFields";

export function Agents() {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const [kind, setKind] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const list = useCursorList<Agent>(withQuery("/api/v1/ai/agents", { kind }));
  const columns: Column<Agent>[] = [
    { key: "name", header: "Agent", cell: (a) => <Link className="font-medium text-brand hover:underline" href={`/app/agents/${a.id}`}>{a.name}</Link> },
    { key: "kind", header: "Kind", cell: (a) => <Badge>{a.kind}</Badge> },
    { key: "status", header: "Status", cell: (a) => <StatusBadge status={a.status} /> },
    { key: "version", header: "Live version", cell: (a) => <span className="tabular">{a.current_version ? `v${a.current_version}` : "none"}{a.latest_version > (a.current_version ?? 0) ? <span className="ml-1 text-xs text-muted">(latest v{a.latest_version})</span> : null}</span> },
    { key: "updated", header: "Updated", cell: (a) => <span title={formatDateTime(a.updated_at)}>{relativeTime(a.updated_at)}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader
        title="AI agents"
        description="Voice agents call contacts; email agents draft replies for review. Configuration changes go through immutable, activated versions."
        actions={can("ai:agents:manage") ? <Button size="sm" onClick={() => setCreating(true)} data-testid="new-agent">New agent</Button> : null}
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
        <FilterSelect id="agent-kind" label="Kind" value={kind} onChange={setKind} options={["voice", "email"]} />
      </div>
      <DataTable caption="AI agents" columns={columns} rows={list.items} rowKey={(a) => a.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No agents yet" />} />
      <CreateAgentDialog open={creating} onClose={() => setCreating(false)} onCreated={(a) => { setCreating(false); toast({ tone: "success", title: "Agent created", description: "Review version 1 and activate it to go live." }); router.push(`/app/agents/${a.id}`); }} />
    </Card>
  );
}

function CreateAgentDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (a: Agent) => void }) {
  const sources = useApi<Items<KnowledgeSource>>(open ? "/api/v1/ai/knowledge/sources?limit=100" : null);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [kind, setKind] = React.useState("voice");
  const [form, setForm] = React.useState<VersionForm>(emptyVersionForm);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (!open) return;
    setName("");
    setDescription("");
    setKind("voice");
    setForm({ ...emptyVersionForm(), change_note: "Initial version" });
    setErrors({});
    setError(null);
    key.current = newIdempotencyKey();
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const b = buildVersion(form, kind);
    const local = { ...b.errors };
    if (name.trim().length < 2) local.name = "At least 2 characters";
    setErrors(local);
    setError(null);
    if (!b.body || Object.keys(local).length) return;
    setBusy(true);
    try {
      onCreated(await api<Agent>("/api/v1/ai/agents", { method: "POST", body: { name: name.trim(), description: description.trim(), kind, config: b.body }, idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const merged = mergeErrors(errors, error?.fields, "config.");
  const fieldErr = (k: string) => merged[k];
  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="xl" title="New AI agent" description="Version 1 is created with the agent. It is not live until someone with agent management rights activates it.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field id="agent-name" label="Name" required error={fieldErr("name")}>
            {(p) => <Input {...p} maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="agent-kind-new" label="Kind" error={fieldErr("kind")}>
            {(p) => <Select {...p} value={kind} onChange={(e) => { setKind(e.target.value); setForm((f) => ({ ...f, tool_ids: [] })); }}><option value="voice">Voice (calls)</option><option value="email">Email (drafting)</option></Select>}
          </Field>
          <Field id="agent-desc" label="Description" error={fieldErr("description")}>
            {(p) => <Textarea {...p} rows={1} className="min-h-[36px]" maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        <VersionFields form={form} setForm={setForm} errors={merged} kind={kind} sources={sources.data?.items ?? []} />
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy} data-testid="create-agent">{busy ? "Creating…" : "Create agent"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

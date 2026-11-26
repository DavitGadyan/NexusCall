"use client";
/**
 * Evaluations: datasets of test cases, runs of a dataset (plus the built-in injection and poisoning suite)
 * against an agent version, and per-case results. Case text is shown as plain text only.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingState,
  PageHeader,
  Select,
  Stat,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { JobBadge, MutationError } from "@/components/crm/common";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import { usePoll } from "@/components/crm/lib/use-poll";
import type { Items } from "@/components/crm/lib/types";
import type { Agent, AgentVersion, Dataset, EvalCase, EvalRun } from "./lib/types";

const RUN_DONE = new Set(["completed", "failed", "canceled"]);

export function passRate(run: Pick<EvalRun, "passed" | "failed">): number | null {
  const total = run.passed + run.failed;
  return total ? Math.round((run.passed / total) * 100) : null;
}

export function Datasets() {
  const can = useCan();
  const list = useCursorList<Dataset>("/api/v1/ai/evals/datasets");
  const [open, setOpen] = React.useState(false);
  const columns: Column<Dataset>[] = [
    { key: "name", header: "Dataset", cell: (d) => <Link className="font-medium text-brand hover:underline" href={`/app/agents/evals/${d.id}`}>{d.name}</Link> },
    { key: "desc", header: "Description", cell: (d) => <span className="line-clamp-1 text-muted">{d.description || "—"}</span> },
    { key: "updated", header: "Updated", cell: (d) => <span title={formatDateTime(d.updated_at)}>{relativeTime(d.updated_at)}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader title="Evaluation datasets" description="Reusable test cases for agents. Every run also includes the built-in prompt-injection and knowledge-poisoning cases unless you turn them off." actions={can("ai:agents:manage") ? <Button size="sm" onClick={() => setOpen(true)} data-testid="new-dataset">New dataset</Button> : null} />
      <DataTable caption="Datasets" columns={columns} rows={list.items} rowKey={(d) => d.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No datasets yet" />} />
      <DatasetDialog open={open} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); list.reload(); }} />
    </Card>
  );
}

function DatasetDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/ai/evals/datasets", { method: "POST", body: { name: name.trim(), description: description.trim() } });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="New dataset">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="ds-name" label="Name" required error={error?.fields.name}>
          {(p) => <Input {...p} minLength={2} maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field id="ds-desc" label="Description" error={error?.fields.description}>
          {(p) => <Textarea {...p} maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />}
        </Field>
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || name.trim().length < 2}>{busy ? "Creating…" : "Create"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

export function DatasetDetail({ datasetId }: { datasetId: string }) {
  const ds = useApi<Dataset>(`/api/v1/ai/evals/datasets/${datasetId}`);
  const [tab, setTab] = React.useState("runs");
  if (ds.loading && !ds.data) return <Card><LoadingState /></Card>;
  if (ds.error && !ds.data) return <Card><ErrorState problem={ds.error.problem} onRetry={ds.reload} /></Card>;
  if (!ds.data) return null;
  return (
    <>
      <PageHeader title={ds.data.name} description={ds.data.description || undefined} />
      <Tabs label="Dataset sections" value={tab} onChange={setTab} tabs={[{ id: "runs", label: "Runs" }, { id: "cases", label: "Cases" }]}>
        {tab === "runs" ? <Runs datasetId={datasetId} /> : <Cases datasetId={datasetId} />}
      </Tabs>
    </>
  );
}

function Cases({ datasetId }: { datasetId: string }) {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<EvalCase>(`/api/v1/ai/evals/datasets/${datasetId}/cases`);
  const [open, setOpen] = React.useState(false);
  const [removing, setRemoving] = React.useState<EvalCase | null>(null);
  const manage = can("ai:agents:manage");
  const columns: Column<EvalCase>[] = [
    { key: "name", header: "Case", cell: (c) => <span className="font-medium">{c.name}</span> },
    { key: "kind", header: "Kind", cell: (c) => <Badge tone={c.kind === "quality" ? "neutral" : "warn"}>{c.kind}</Badge> },
    { key: "source", header: "Input as", cell: (c) => humanize(c.source), hideOnMobile: true },
    { key: "input", header: "Input", cell: (c) => <span className="line-clamp-2 whitespace-pre-wrap break-words text-muted">{c.input_text}</span> },
    { key: "actions", header: <span className="sr-only">Actions</span>, className: "text-right", cell: (c) => (manage ? <Button size="sm" variant="ghost" onClick={() => setRemoving(c)}>Delete</Button> : null) },
  ];
  return (
    <Card>
      <CardHeader title="Cases" actions={manage ? <Button size="sm" onClick={() => setOpen(true)} data-testid="new-case">New case</Button> : null} />
      <DataTable caption="Cases" columns={columns} rows={list.items} rowKey={(c) => c.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No cases yet" description="Runs still include the built-in security cases." />} />
      <CaseDialog open={open} datasetId={datasetId} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); list.reload(); toast({ tone: "success", title: "Case added" }); }} />
      <ConfirmDialog open={removing !== null} onClose={() => setRemoving(null)} title="Delete case" description="Past run results keep their record of this case." confirmLabel="Delete" onConfirm={async () => {
        if (!removing) return;
        await api(`/api/v1/ai/evals/datasets/${datasetId}/cases/${removing.id}`, { method: "DELETE" });
        list.reload();
      }} />
    </Card>
  );
}

const FLAGS = ["prompt_injection_suspected", "input_secret_redacted", "output_blocked"] as const;

function lines(text: string): string[] {
  return text.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 20);
}

function CaseDialog({ open, datasetId, onClose, onSaved }: { open: boolean; datasetId: string; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = React.useState({ name: "", kind: "quality", source: "email", input_text: "", knowledge_text: "", required: "", forbidden: "", leak: false, flags: [] as string[] });
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setF({ name: "", kind: "quality", source: "email", input_text: "", knowledge_text: "", required: "", forbidden: "", leak: false, flags: [] });
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/ai/evals/datasets/${datasetId}/cases`, {
        method: "POST",
        body: {
          name: f.name.trim(),
          kind: f.kind,
          source: f.source,
          input_text: f.input_text,
          knowledge_text: f.knowledge_text || null,
          expectations: { required_phrases: lines(f.required), forbidden_phrases: lines(f.forbidden), check_system_prompt_leak: f.leak, expect_flags: f.flags },
        },
      });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="lg" title="New evaluation case">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field id="case-name" label="Name" required error={error?.fields.name}>
            {(p) => <Input {...p} minLength={2} maxLength={120} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
          </Field>
          <Field id="case-kind" label="Kind" error={error?.fields.kind}>
            {(p) => <Select {...p} value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}><option value="quality">Quality</option><option value="injection">Prompt injection</option><option value="poisoning">Knowledge poisoning</option></Select>}
          </Field>
          <Field id="case-source" label="Input arrives as" error={error?.fields.source}>
            {(p) => <Select {...p} value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>{["email", "transcript", "document", "knowledge", "user"].map((s) => <option key={s} value={s}>{humanize(s)}</option>)}</Select>}
          </Field>
        </div>
        <Field id="case-input" label="Input text" required hint="Treated as untrusted input during the run." error={error?.fields.input_text}>
          {(p) => <Textarea {...p} rows={5} maxLength={20000} value={f.input_text} onChange={(e) => setF({ ...f, input_text: e.target.value })} />}
        </Field>
        <Field id="case-knowledge" label="Knowledge text (optional)" error={error?.fields.knowledge_text}>
          {(p) => <Textarea {...p} rows={3} maxLength={20000} value={f.knowledge_text} onChange={(e) => setF({ ...f, knowledge_text: e.target.value })} />}
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="case-required" label="Required phrases" hint="One per line." error={error?.fields["expectations.required_phrases"]}>
            {(p) => <Textarea {...p} rows={3} value={f.required} onChange={(e) => setF({ ...f, required: e.target.value })} />}
          </Field>
          <Field id="case-forbidden" label="Forbidden phrases" hint="One per line." error={error?.fields["expectations.forbidden_phrases"]}>
            {(p) => <Textarea {...p} rows={3} value={f.forbidden} onChange={(e) => setF({ ...f, forbidden: e.target.value })} />}
          </Field>
        </div>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Security expectations</legend>
          <Checkbox label="Fail if the system prompt leaks" checked={f.leak} onChange={(e) => setF({ ...f, leak: e.target.checked })} />
          {FLAGS.map((fl) => <Checkbox key={fl} label={`Expect flag: ${humanize(fl)}`} checked={f.flags.includes(fl)} onChange={(e) => setF({ ...f, flags: e.target.checked ? [...f.flags, fl] : f.flags.filter((x) => x !== fl) })} />)}
        </fieldset>
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || f.name.trim().length < 2 || !f.input_text.trim()}>{busy ? "Saving…" : "Add case"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function Runs({ datasetId }: { datasetId: string }) {
  const can = useCan();
  const list = useCursorList<EvalRun>(`/api/v1/ai/evals/datasets/${datasetId}/runs`);
  const [open, setOpen] = React.useState(false);
  const [shown, setShown] = React.useState<string | null>(null);
  usePoll(list.reload, list.items.some((r) => !RUN_DONE.has(r.status)));
  const run = list.items.find((r) => r.id === shown) ?? null;
  const columns: Column<EvalRun>[] = [
    { key: "when", header: "Run", cell: (r) => <button type="button" className="text-brand hover:underline" onClick={() => setShown(r.id)} aria-pressed={r.id === shown}>{formatDateTime(r.created_at)}</button> },
    { key: "status", header: "Status", cell: (r) => <span><JobBadge status={r.status} />{r.error_code ? <span className="block text-xs text-danger">{humanize(r.error_code)}</span> : null}</span> },
    { key: "agent", header: "Agent version", cell: (r) => <Link className="text-brand hover:underline" href={`/app/agents/${r.agent_id}`}>v{r.prompt_version}</Link> },
    { key: "score", header: "Passed", cell: (r) => <span className="tabular">{r.passed}/{r.case_count}{passRate(r) !== null ? <span className="ml-1 text-xs text-muted">({passRate(r)}%)</span> : null}</span> },
    { key: "sec", header: "Security suite", cell: (r) => (r.include_security_cases ? "Included" : "Off"), hideOnMobile: true },
  ];
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader title="Runs" actions={can("ai:agents:manage") ? <Button size="sm" onClick={() => setOpen(true)} data-testid="start-run">Start run</Button> : null} />
        <DataTable caption="Evaluation runs" columns={columns} rows={list.items} rowKey={(r) => r.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No runs yet" />} />
      </Card>
      {run ? <RunResults run={run} /> : null}
      <RunDialog open={open} datasetId={datasetId} onClose={() => setOpen(false)} onStarted={(r) => { setOpen(false); list.reload(); setShown(r.id); }} />
    </div>
  );
}

function RunResults({ run }: { run: EvalRun }) {
  const failedFirst = [...run.results].sort((a, b) => Number(a.passed) - Number(b.passed));
  return (
    <Card>
      <CardHeader title="Results" description={`Run of ${formatDateTime(run.created_at)}${run.finished_at ? `, finished ${relativeTime(run.finished_at)}` : ""}.`} />
      <div className="grid gap-4 px-5 py-4 sm:grid-cols-3">
        <Stat label="Cases" value={run.case_count} />
        <Stat label="Passed" value={run.passed} />
        <Stat label="Failed" value={run.failed} />
      </div>
      {failedFirst.length === 0 ? <EmptyState title={RUN_DONE.has(run.status) ? "No results" : "Waiting for results…"} /> : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Per-case results</caption>
            <thead><tr className="text-xs uppercase tracking-wide text-muted"><th scope="col" className="border-b border-border px-4 py-2">Case</th><th scope="col" className="border-b border-border px-4 py-2">Kind</th><th scope="col" className="border-b border-border px-4 py-2">Result</th><th scope="col" className="border-b border-border px-4 py-2">Why</th><th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Flags</th></tr></thead>
            <tbody>
              {failedFirst.map((r, i) => (
                <tr key={`${r.case}-${i}`} className="align-top">
                  <td className="border-b border-border px-4 py-2 font-mono text-xs">{r.case}</td>
                  <td className="border-b border-border px-4 py-2">{r.kind}</td>
                  <td className="border-b border-border px-4 py-2">{r.passed ? <Badge tone="ok">passed</Badge> : <Badge tone="danger">failed</Badge>}</td>
                  <td className="border-b border-border px-4 py-2 text-xs">{r.failed.length ? r.failed.map(humanize).join(", ") : "—"}</td>
                  <td className="hidden border-b border-border px-4 py-2 text-xs md:table-cell">{[...(r.flags ?? []), ...(r.tool_statuses ?? []).map((t) => `tool ${t}`)].map(humanize).join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function RunDialog({ open, datasetId, onClose, onStarted }: { open: boolean; datasetId: string; onClose: () => void; onStarted: (r: EvalRun) => void }) {
  const agents = useApi<Items<Agent>>(open ? withQuery("/api/v1/ai/agents", { limit: 100 }) : null);
  const [agentId, setAgentId] = React.useState("");
  const versions = useApi<Items<AgentVersion>>(open && agentId ? `/api/v1/ai/agents/${agentId}/versions?limit=100` : null);
  const [version, setVersion] = React.useState("");
  const [security, setSecurity] = React.useState(true);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (open) {
      setAgentId("");
      setVersion("");
      setSecurity(true);
      setError(null);
      key.current = newIdempotencyKey();
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onStarted(await api<EvalRun>(`/api/v1/ai/evals/datasets/${datasetId}/runs`, { method: "POST", body: { agent_id: agentId, prompt_version: version ? Number(version) : null, include_security_cases: security }, idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  const agent = agents.data?.items.find((a) => a.id === agentId);
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Start evaluation run" description="Runs use the AI gateway like production traffic and count towards usage.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="run-agent" label="Agent" required error={error?.fields.agent_id}>
          {(p) => <Select {...p} value={agentId} onChange={(e) => { setAgentId(e.target.value); setVersion(""); }}><option value="">{agents.loading ? "Loading…" : "Choose…"}</option>{(agents.data?.items ?? []).filter((a) => a.status !== "archived").map((a) => <option key={a.id} value={a.id}>{a.name} ({a.kind})</option>)}</Select>}
        </Field>
        <Field id="run-version" label="Version" hint={agent?.current_version ? `Empty: the live version (v${agent.current_version}).` : "Empty: the live version."} error={error?.fields.prompt_version}>
          {(p) => <Select {...p} value={version} disabled={!agentId} onChange={(e) => setVersion(e.target.value)}><option value="">Live version</option>{(versions.data?.items ?? []).map((v) => <option key={v.version} value={v.version}>v{v.version}{v.change_note ? ` — ${v.change_note.slice(0, 40)}` : ""}</option>)}</Select>}
        </Field>
        <Checkbox label="Include the built-in injection and poisoning cases" checked={security} onChange={(e) => setSecurity(e.target.checked)} />
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || !agentId}>{busy ? "Starting…" : "Start run"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

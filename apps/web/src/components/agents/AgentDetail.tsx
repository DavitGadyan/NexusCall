"use client";
/**
 * Agent detail: overview (rename, archive), prompt versions as an immutable history with a diff view,
 * new version (always a new number, never an edit) and activation, tool grants, recent tool executions.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime, shortId } from "@/lib/format";
import {
  Badge,
  Button,
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
import { MutationError } from "@/components/crm/common";
import { FilterSelect } from "@/components/crm/Contacts";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import type { Items } from "@/components/crm/lib/types";
import { compareVersions, diffStats, TEXT_FIELD_LABEL, withContext } from "./lib/diff";
import { buildVersion, emptyVersionForm, mergeErrors, type VersionForm, versionToForm } from "./lib/version-form";
import type { Agent, AgentVersion, Execution, KnowledgeSource } from "./lib/types";
import { ToolGrants } from "./ToolGrants";
import { VersionFields } from "./VersionFields";

export function AgentDetail({ agentId }: { agentId: string }) {
  const can = useCan();
  const agent = useApi<Agent>(`/api/v1/ai/agents/${agentId}`);
  const [tab, setTab] = React.useState("versions");
  if (agent.loading && !agent.data) return <Card><LoadingState /></Card>;
  if (agent.error && !agent.data) return <Card><ErrorState problem={agent.error.problem} onRetry={agent.reload} /></Card>;
  const a = agent.data;
  if (!a) return null;
  const archived = a.status === "archived";
  return (
    <>
      <PageHeader title={a.name} description={a.description || undefined} />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <Badge>{a.kind}</Badge>
        <StatusBadge status={a.status} />
        <span className="text-muted">Live: {a.current_version ? `v${a.current_version}` : "no version active"} · latest v{a.latest_version}</span>
      </div>
      {archived ? <div className="mb-4"><Notice tone="warn" title="Archived">Archived agents keep their history but cannot change or be activated.</Notice></div> : null}
      <Tabs
        label="Agent sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "versions", label: "Versions" },
          { id: "grants", label: "Tool grants" },
          ...(can("ai:usage:read") ? [{ id: "executions", label: "Tool executions" }] : []),
          { id: "settings", label: "Settings" },
        ]}
      >
        {tab === "versions" ? <Versions agent={a} onAgentChanged={(next) => agent.setData(() => next)} /> : null}
        {tab === "grants" ? <ToolGrants agent={a} /> : null}
        {tab === "executions" ? <Executions agentId={a.id} /> : null}
        {tab === "settings" ? <AgentSettings agent={a} onSaved={(next) => agent.setData(() => next)} onReload={agent.reload} /> : null}
      </Tabs>
    </>
  );
}

function Versions({ agent, onAgentChanged }: { agent: Agent; onAgentChanged: (a: Agent) => void }) {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<AgentVersion>(`/api/v1/ai/agents/${agent.id}/versions`);
  const [selected, setSelected] = React.useState<number | null>(null);
  /** undefined = default (the previous version), null = no comparison */
  const [compareTo, setCompareTo] = React.useState<number | null | undefined>(undefined);
  const [composing, setComposing] = React.useState<AgentVersion | "blank" | null>(null);
  const [activating, setActivating] = React.useState<AgentVersion | null>(null);
  const versions = [...list.items].sort((x, y) => y.version - x.version);
  const current = selected ?? agent.current_version ?? versions[0]?.version ?? null;
  const shown = versions.find((v) => v.version === current) ?? null;
  const cmpVersion = compareTo === undefined ? (shown ? versions.find((v) => v.version < shown.version)?.version ?? null : null) : compareTo;
  const other = cmpVersion !== null ? versions.find((v) => v.version === cmpVersion) ?? null : null;
  const manage = can("ai:agents:manage") && agent.status !== "archived";

  const columns: Column<AgentVersion>[] = [
    {
      key: "v",
      header: "Version",
      cell: (v) => (
        <button type="button" onClick={() => { setSelected(v.version); setCompareTo(undefined); }} className={v.version === current ? "font-semibold text-fg" : "text-brand hover:underline"} aria-pressed={v.version === current}>
          v{v.version}
        </button>
      ),
    },
    { key: "live", header: "State", cell: (v) => (v.version === agent.current_version ? <Badge tone="ok">live</Badge> : <span className="text-xs text-muted">inactive</span>) },
    { key: "note", header: "Change note", cell: (v) => <span className="line-clamp-1">{v.change_note || "—"}</span> },
    { key: "by", header: "Created", cell: (v) => <span title={formatDateTime(v.created_at)}>{relativeTime(v.created_at)}<span className="block font-mono text-xs text-muted">{v.created_by ?? ""}</span></span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (v) =>
        manage ? (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => setComposing(v)}>New from this</Button>
            {v.version !== agent.current_version ? <Button size="sm" onClick={() => setActivating(v)} data-testid={`activate-v${v.version}`}>Activate</Button> : null}
          </div>
        ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader
          title="Version history"
          description="Versions are append-only and never edited. A new version takes effect only when activated; activation is audited."
          actions={manage ? <Button size="sm" onClick={() => setComposing(shown ?? "blank")} data-testid="new-version">New version</Button> : null}
        />
        <DataTable caption="Prompt versions" columns={columns} rows={versions} rowKey={(v) => v.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No versions" />} />
      </Card>
      {shown ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <VersionView v={shown} live={shown.version === agent.current_version} />
          <Card>
            <CardHeader
              title="Compare"
              actions={
                <div className="flex items-center gap-2">
                  <label htmlFor="cmp-base" className="text-sm text-muted">v{shown.version} against</label>
                  <Select id="cmp-base" className="w-28" value={cmpVersion ?? ""} onChange={(e) => setCompareTo(e.target.value ? Number(e.target.value) : null)}>
                    <option value="">—</option>
                    {versions.filter((v) => v.version !== shown.version).map((v) => <option key={v.version} value={v.version}>v{v.version}</option>)}
                  </Select>
                </div>
              }
            />
            {other ? <VersionDiff before={other} after={shown} /> : <p className="px-5 py-6 text-sm text-muted">Choose a version to compare.</p>}
          </Card>
        </div>
      ) : null}
      <NewVersionDialog
        agent={agent}
        from={composing}
        onClose={() => setComposing(null)}
        onCreated={(v) => {
          setComposing(null);
          list.reload();
          setSelected(v.version);
          setCompareTo(undefined);
          toast({ tone: "success", title: `Version ${v.version} created`, description: "It is not live until you activate it." });
        }}
      />
      <ConfirmDialog
        open={activating !== null}
        onClose={() => setActivating(null)}
        title={`Activate version ${activating?.version ?? ""}`}
        tone="primary"
        description={`New calls${agent.kind === "email" ? " and drafts" : ""} use v${activating?.version ?? ""} from now on. Calls in progress keep the version they started with. You can activate another version at any time.`}
        confirmLabel="Activate"
        onConfirm={async () => {
          if (!activating) return;
          const next = await api<Agent>(`/api/v1/ai/agents/${agent.id}/versions/${activating.version}/activate`, { method: "POST" });
          onAgentChanged(next);
          toast({ tone: "success", title: `Version ${activating.version} is live` });
        }}
      />
    </div>
  );
}

function VersionView({ v, live }: { v: AgentVersion; live: boolean }) {
  return (
    <Card>
      <CardHeader title={`Version ${v.version}`} description={v.change_note || "No change note"} actions={live ? <Badge tone="ok">live</Badge> : <Badge>read-only</Badge>} />
      <div className="flex flex-col gap-4 px-5 py-4">
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">System prompt</p>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-2 p-3 font-mono text-xs">{v.system_prompt}</pre>
        </div>
        <KeyValues
          items={[
            { label: "Greeting", value: v.greeting || "—" },
            { label: "AI disclosure", value: v.disclosure_text || "—" },
            { label: "Model", value: v.model ?? "Workspace default" },
            { label: "Temperature", value: v.temperature ?? "Default" },
            { label: "Voice", value: v.voice_id ?? "—" },
            { label: "Max call", value: `${v.max_call_seconds} s` },
            { label: "Tools", value: v.tool_ids.length ? <span className="flex flex-wrap gap-1">{v.tool_ids.map((t) => <code key={t} className="rounded bg-surface-2 px-1 text-xs">{t}</code>)}</span> : "None" },
            { label: "Knowledge", value: v.knowledge_source_ids.length ? `${v.knowledge_source_ids.length} source(s)` : "None" },
            { label: "Escalation", value: v.escalation_rules.length ? v.escalation_rules.map((r) => `${humanize(r.trigger)}${r.keyword ? ` "${r.keyword}"` : ""} → ${humanize(r.action)}`).join("; ") : "None" },
            { label: "Created", value: `${formatDateTime(v.created_at)}${v.created_by ? ` by ${v.created_by}` : ""}` },
          ]}
        />
      </div>
    </Card>
  );
}

export function VersionDiff({ before, after }: { before: AgentVersion; after: AgentVersion }) {
  const cmp = React.useMemo(() => compareVersions(before, after), [before, after]);
  if (cmp.text.length === 0 && cmp.settings.length === 0) return <p className="px-5 py-6 text-sm text-muted">v{before.version} and v{after.version} are identical.</p>;
  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      {cmp.settings.length ? (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Changed settings</caption>
          <thead><tr className="text-muted"><th scope="col" className="py-1 pr-3">Setting</th><th scope="col" className="py-1 pr-3">v{before.version}</th><th scope="col" className="py-1">v{after.version}</th></tr></thead>
          <tbody>
            {cmp.settings.map((s) => (
              <tr key={s.label} className="border-t border-border align-top">
                <th scope="row" className="py-1 pr-3 font-medium">{s.label}</th>
                <td className="py-1 pr-3 text-danger line-through decoration-danger/50">{s.before}</td>
                <td className="py-1 text-ok">{s.after}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {cmp.text.map(({ field, lines }) => {
        const stats = diffStats(lines);
        return (
          <div key={field}>
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
              {TEXT_FIELD_LABEL[field]} <span className="ml-2 normal-case text-ok">+{stats.added}</span> <span className="normal-case text-danger">−{stats.removed}</span>
            </p>
            <div className="max-h-96 overflow-auto rounded-md border border-border font-mono text-xs" role="table" aria-label={`${TEXT_FIELD_LABEL[field]} changes`}>
              {withContext(lines).map((l, i) =>
                l.kind === "gap" ? (
                  <div key={i} role="row" className="bg-surface-2 px-3 py-0.5 text-muted">… {l.count} unchanged line{l.count === 1 ? "" : "s"}</div>
                ) : (
                  <div key={i} role="row" className={l.kind === "add" ? "flex bg-ok/10" : l.kind === "del" ? "flex bg-danger/10" : "flex"}>
                    <span role="cell" className="w-6 shrink-0 select-none text-center text-muted" aria-label={l.kind === "add" ? "added" : l.kind === "del" ? "removed" : "unchanged"}>{l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span>
                    <span role="cell" className="min-w-0 flex-1 whitespace-pre-wrap break-words pr-3">{l.text || " "}</span>
                  </div>
                ),
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function NewVersionDialog({ agent, from, onClose, onCreated }: { agent: Agent; from: AgentVersion | "blank" | null; onClose: () => void; onCreated: (v: AgentVersion) => void }) {
  const sources = useApi<Items<KnowledgeSource>>(from ? "/api/v1/ai/knowledge/sources?limit=100" : null);
  const [form, setForm] = React.useState<VersionForm>(emptyVersionForm);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (!from) return;
    setForm(from === "blank" ? emptyVersionForm() : versionToForm(from));
    setErrors({});
    setError(null);
    key.current = newIdempotencyKey();
  }, [from]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const b = buildVersion(form, agent.kind);
    setErrors(b.errors);
    setError(null);
    if (!b.body) return;
    setBusy(true);
    try {
      onCreated(await api<AgentVersion>(`/api/v1/ai/agents/${agent.id}/versions`, { method: "POST", body: b.body, idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={from !== null}
      onClose={onClose}
      busy={busy}
      size="xl"
      title={`New version (v${agent.latest_version + 1})`}
      description={from && from !== "blank" ? `Starts from a copy of v${from.version}. The original stays unchanged.` : "Starts empty."}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        <VersionFields form={form} setForm={setForm} errors={mergeErrors(errors, error?.fields)} kind={agent.kind} sources={sources.data?.items ?? []} />
        <MutationError error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy} data-testid="create-version">{busy ? "Creating…" : "Create version"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function Executions({ agentId }: { agentId: string }) {
  const [status, setStatus] = React.useState("");
  const list = useCursorList<Execution>(withQuery("/api/v1/ai/tool-executions", { agent_id: agentId, status }));
  const columns: Column<Execution>[] = [
    { key: "tool", header: "Tool", cell: (x) => <code className="text-xs">{x.tool_id}</code> },
    { key: "status", header: "Status", cell: (x) => <span><StatusBadge status={x.status} />{x.reason ? <span className="block text-xs text-muted">{humanize(x.reason)}</span> : null}</span> },
    { key: "risk", header: "Risk", cell: (x) => (x.risk_class === null ? "—" : `Class ${x.risk_class}`), hideOnMobile: true },
    { key: "approval", header: "Approval", cell: (x) => (x.approval_id ? <Link className="font-mono text-xs text-brand hover:underline" href="/app/approvals">{shortId(x.approval_id)}</Link> : "—"), hideOnMobile: true },
    { key: "target", header: "Context", cell: (x) => (x.call_id ? <Link className="text-xs text-brand hover:underline" href={`/app/calls/${x.call_id}`}>Call {shortId(x.call_id)}</Link> : x.thread_id ? `Thread ${shortId(x.thread_id)}` : "—"), hideOnMobile: true },
    { key: "ms", header: "Duration", cell: (x) => <span className="tabular">{x.duration_ms} ms</span>, hideOnMobile: true },
    { key: "at", header: "When", cell: (x) => <span title={formatDateTime(x.created_at)}>{relativeTime(x.created_at)}</span> },
  ];
  return (
    <Card>
      <CardHeader title="Tool executions" description="Every tool call this agent requested, with the policy outcome. Arguments and results are not shown here." />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
        <FilterSelect id="exec-status" label="Status" value={status} onChange={setStatus} options={["executed", "approval_required", "denied", "failed"]} />
      </div>
      <DataTable caption="Tool executions" columns={columns} rows={list.items} rowKey={(x) => x.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No tool executions" />} />
    </Card>
  );
}

function AgentSettings({ agent, onSaved, onReload }: { agent: Agent; onSaved: (a: Agent) => void; onReload: () => void }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const [name, setName] = React.useState(agent.name);
  const [description, setDescription] = React.useState(agent.description);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [archiving, setArchiving] = React.useState(false);
  React.useEffect(() => {
    setName(agent.name);
    setDescription(agent.description);
  }, [agent]);
  const manage = can("ai:agents:manage") && agent.status !== "archived";
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { version: agent.version };
      if (name.trim() !== agent.name) body.name = name.trim();
      if (description.trim() !== agent.description) body.description = description.trim();
      onSaved(await api<Agent>(`/api/v1/ai/agents/${agent.id}`, { method: "PATCH", body }));
      toast({ tone: "success", title: "Agent saved" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader title="Settings" description="Name and description only. Behaviour changes go through a new version." />
      <form onSubmit={save} className="flex flex-col gap-4 px-5 py-4">
        <Field id="agent-name-edit" label="Name" error={error?.fields.name}>
          {(p) => <Input {...p} maxLength={120} disabled={!manage} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field id="agent-desc-edit" label="Description" error={error?.fields.description}>
          {(p) => <Textarea {...p} maxLength={500} disabled={!manage} value={description} onChange={(e) => setDescription(e.target.value)} />}
        </Field>
        <KeyValues items={[{ label: "Kind", value: agent.kind }, { label: "Created", value: formatDateTime(agent.created_at) }, { label: "ID", value: <code className="text-xs">{agent.id}</code> }]} />
        <MutationError error={error} onReload={() => { setError(null); onReload(); }} />
        {manage ? (
          <div className="flex justify-between gap-2">
            <Button type="button" variant="ghost" onClick={() => setArchiving(true)}>Archive agent</Button>
            <Button type="submit" disabled={busy || name.trim().length < 2 || (name.trim() === agent.name && description.trim() === agent.description)}>{busy ? "Saving…" : "Save"}</Button>
          </div>
        ) : null}
      </form>
      <ConfirmDialog
        open={archiving}
        onClose={() => setArchiving(false)}
        title="Archive agent"
        description="The agent stops taking new calls or drafts. Campaigns using it cannot launch. Its version history is kept."
        confirmLabel="Archive"
        typedConfirmation={agent.name}
        onConfirm={async () => {
          await api(`/api/v1/ai/agents/${agent.id}`, { method: "DELETE" });
          toast({ tone: "success", title: "Agent archived" });
          router.push("/app/agents");
        }}
      />
    </Card>
  );
}

"use client";
/**
 * AI approvals: actions an AI agent proposed that wait for a person (build.md §24). The queue updates from
 * `ai.approval.created` hints. Approving needs step-up MFA and runs the action exactly once; the backend
 * re-checks the grant and the bound context at that moment.
 */
import { ShieldCheck } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  type Column,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  KeyValues,
  LoadingState,
  Notice,
  PageHeader,
  Select,
  StatusBadge,
  Tabs,
  Textarea,
  useRecentStepUp,
  useToast,
} from "@/components/ui";
import { useAgentNames } from "@/components/calls/pickers";
import { useLiveHint, useLiveRefresh, useNow } from "@/components/calls/use-live";
import { type Approval, APPROVAL_STATUSES, argText, contextLinks, type DecisionOut, effectiveStatus, executionMessage, riskLabel, toolLabel } from "./approvals";

export function ApprovalQueue({ initialTab }: { initialTab?: string }) {
  const [tab, setTab] = React.useState(initialTab === "history" ? "history" : "queue");
  return (
    <Tabs
      label="AI approvals"
      value={tab}
      onChange={setTab}
      tabs={[
        { id: "queue", label: "Waiting for a decision" },
        { id: "history", label: "History" },
      ]}
    >
      {tab === "queue" ? <PendingList /> : <HistoryList />}
    </Tabs>
  );
}

function useColumns(now: number): Column<Approval>[] {
  const agentName = useAgentNames();
  return [
    {
      key: "tool",
      header: "Proposed action",
      cell: (a) => (
        <Link href={`/app/approvals/${a.id}`} className="block text-brand hover:underline">
          <span className="font-medium">{toolLabel(a.tool_id)}</span>
          <span className="block text-xs text-muted">by {agentName(a.agent_id)}</span>
        </Link>
      ),
    },
    {
      key: "for",
      header: "For",
      cell: (a) => {
        const ctx = contextLinks(a.context).filter((c) => c.label === "Contact" || c.label === "Call" || c.label === "Email thread");
        return ctx.length ? <span className="text-sm">{ctx.map((c) => c.label).join(", ")}</span> : <span className="text-muted">—</span>;
      },
    },
    {
      key: "risk",
      header: "Risk",
      cell: (a) => {
        const r = riskLabel(a.risk_class);
        return <Badge tone={r.tone}>{r.label}</Badge>;
      },
      hideOnMobile: true,
    },
    { key: "status", header: "Status", cell: (a) => <StatusBadge status={effectiveStatus(a, now)} /> },
    {
      key: "time",
      header: "Requested",
      cell: (a) => (
        <span title={formatDateTime(a.created_at)}>
          {relativeTime(a.created_at, now)}
          {effectiveStatus(a, now) === "pending" ? <span className="block text-xs text-warn">expires {relativeTime(a.expires_at, now)}</span> : null}
        </span>
      ),
    },
  ];
}

function PendingList() {
  const list = useCursorList<Approval>("/api/v1/ai/tool-approvals?status=pending");
  useLiveRefresh(list.reload, { events: ["ai.approval.created"], pollMs: 30_000 });
  const now = useNow(15_000);
  const columns = useColumns(now);
  return (
    <Card>
      <CardHeader title="Waiting for a decision" description="Oldest requests expire first. Nothing here runs until someone approves it." />
      <DataTable
        caption="Pending AI approvals"
        columns={[
          ...columns,
          {
            key: "review",
            header: <span className="sr-only">Review</span>,
            className: "text-right",
            cell: (a) => (
              <Link href={`/app/approvals/${a.id}`} className="inline-flex h-8 items-center rounded-md border border-border px-3 text-sm hover:bg-surface-2">
                Review
              </Link>
            ),
          },
        ]}
        rows={list.items}
        rowKey={(a) => a.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="Nothing is waiting" description="When an AI agent proposes a customer-facing action that needs a person, it appears here." />}
      />
    </Card>
  );
}

function HistoryList() {
  const [status, setStatus] = React.useState<string>("approved");
  const list = useCursorList<Approval>(withQuery("/api/v1/ai/tool-approvals", { status }));
  const hint = useLiveHint({ events: ["ai.approval.created"] });
  const now = useNow(60_000);
  const columns = useColumns(now);
  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="appr-status" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select id="appr-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-44">
            <option value="">All</option>
            {APPROVAL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "consumed" ? "carried out" : s}
              </option>
            ))}
          </Select>
        </div>
        {hint.stale ? (
          <Button size="sm" variant="secondary" onClick={() => { hint.clear(); list.reload(); }}>
            New requests — refresh
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="AI approval history"
        columns={[
          ...columns,
          { key: "decided", header: "Decided", cell: (a) => (a.decided_at ? relativeTime(a.decided_at, now) : "—"), hideOnMobile: true },
          { key: "result", header: "Result", cell: (a) => (a.result_status ? <StatusBadge status={a.result_status} /> : "—"), hideOnMobile: true },
        ]}
        rows={list.items}
        rowKey={(a) => a.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No requests with this status" />}
      />
    </Card>
  );
}

export function ApprovalDetail({ approvalId }: { approvalId: string }) {
  const can = useCan();
  const toast = useToast();
  const res = useApi<Approval>(`/api/v1/ai/tool-approvals/${approvalId}`);
  const agentName = useAgentNames();
  const now = useNow(5000);
  const stepped = useRecentStepUp();
  const [deciding, setDeciding] = React.useState<"approve" | "reject" | null>(null);
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (res.loading && !res.data) return <Card><LoadingState label="Loading request" /></Card>;
  if (res.error && !res.data) return <Card><ErrorState problem={res.error.problem} onRetry={res.reload} /></Card>;
  const a = res.data;
  if (!a) return null;
  const status = effectiveStatus(a, now);
  const risk = riskLabel(a.risk_class);
  const ctx = contextLinks(a.context);
  const args = Object.entries(a.arguments ?? {});
  const decide = can("ai:approvals:decide") && status === "pending";

  async function submit() {
    if (!deciding) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<DecisionOut>(`/api/v1/ai/tool-approvals/${approvalId}/${deciding}`, { method: "POST", body: { note: note.trim() } });
      if (deciding === "approve") toast(executionMessage(out));
      else toast({ tone: "success", title: "Rejected", description: "The agent is told the action was declined." });
      setDeciding(null);
      res.reload();
    } catch (err) {
      const e = toApiError(err);
      if (e.isConflict) {
        setError(`${e.message}. Someone else may have decided first.`);
        res.reload();
      } else if (!e.isStepUp) setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={toolLabel(a.tool_id)}
        description={`Proposed by ${agentName(a.agent_id)} ${relativeTime(a.created_at, now)}. Tool version ${a.tool_version}.`}
        actions={
          decide ? (
            <>
              <Button variant="secondary" onClick={() => { setNote(""); setError(null); setDeciding("reject"); }}>
                Reject
              </Button>
              <Button onClick={() => { setNote(""); setError(null); setDeciding("approve"); }} data-testid="approve-action">
                <ShieldCheck className="h-4 w-4" aria-hidden /> Approve
              </Button>
            </>
          ) : null
        }
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Card>
            <CardHeader title="What the action will do" description="Exactly these arguments are used. The agent cannot change them after approval." />
            <div className="px-5 py-4">
              {args.length === 0 ? (
                <p className="text-sm text-muted">This action takes no arguments.</p>
              ) : (
                <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[minmax(140px,auto)_1fr]">
                  {args.map(([k, v]) => (
                    <React.Fragment key={k}>
                      <dt className="font-mono text-xs text-muted">{k}</dt>
                      <dd className="min-w-0">
                        <pre className="whitespace-pre-wrap break-words font-sans">{argText(v)}</pre>
                      </dd>
                    </React.Fragment>
                  ))}
                </dl>
              )}
            </div>
          </Card>
          <Card>
            <CardHeader title="For whom, and the context" description="Verified by the platform when the agent made the request (not taken from the AI's arguments)." />
            <div className="px-5 py-4">
              {ctx.length === 0 ? (
                <p className="text-sm text-muted">No customer or conversation is attached.</p>
              ) : (
                <KeyValues items={ctx.map((c) => ({ label: c.label, value: c.href ? <Link className="text-brand hover:underline" href={c.href}>Open {c.label.toLowerCase()}</Link> : <span className="font-mono text-xs">{c.value}</span> }))} />
              )}
            </div>
          </Card>
        </div>
        <Card>
          <CardHeader title="Request" />
          <div className="px-5 py-4">
            <KeyValues
              items={[
                { label: "Status", value: <StatusBadge status={status} /> },
                { label: "Risk", value: <Badge tone={risk.tone}>{risk.label}</Badge> },
                { label: "Requested by", value: `${a.requested_by.type.replace(/_/g, " ")} ${a.requested_by.id.slice(0, 12)}` },
                { label: "Expires", value: `${formatDateTime(a.expires_at)} (${relativeTime(a.expires_at, now)})` },
                ...(a.decided_at ? [{ label: "Decided", value: formatDateTime(a.decided_at) }] : []),
                ...(a.decision_note ? [{ label: "Note", value: a.decision_note }] : []),
                ...(a.result_status ? [{ label: "Result", value: <span><StatusBadge status={a.result_status} /> {a.result_reason ? <span className="text-xs text-muted">{a.result_reason}</span> : null}</span> }] : []),
              ]}
            />
            {!can("ai:approvals:decide") ? <p className="mt-3 text-xs text-muted">You can view this request but not decide on it.</p> : null}
          </div>
        </Card>
      </div>
      <Dialog
        open={deciding !== null}
        onClose={() => setDeciding(null)}
        busy={busy}
        title={deciding === "approve" ? "Approve this action?" : "Reject this action?"}
        description={deciding === "approve" ? "It runs once, immediately, with the arguments shown. Your decision is audited." : "The agent is told the action was declined. Your decision is audited."}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeciding(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant={deciding === "approve" ? "primary" : "danger"} onClick={submit} disabled={busy} data-testid="confirm-decision">
              {busy ? "Working…" : deciding === "approve" ? "Approve and run" : "Reject"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm">
            <span className="font-medium">{toolLabel(a.tool_id)}</span> · <Badge tone={risk.tone}>{risk.label}</Badge>
          </p>
          {deciding === "approve" && !stepped ? (
            <Notice tone="info" title="Multi-factor check">
              Approving needs a recent MFA sign-in. If yours is older than five minutes you will be asked to verify, then approve again.
            </Notice>
          ) : null}
          <Field id="decision-note" label="Note (optional, recorded in the audit log)">
            {(p) => <Textarea {...p} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />}
          </Field>
          <FormError message={error} />
        </div>
      </Dialog>
    </>
  );
}

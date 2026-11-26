"use client";
/** Inbox thread list, thread view and the AI-draft approval queue. */
import { Bot } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Membership, Page } from "@/lib/types";
import { useCan, useSession } from "@/components/shell/session-context";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, EmptyState, ErrorState, LoadingState, PageHeader, Select, useToast } from "@/components/ui";
import { useLiveHint, useLiveRefresh } from "@/components/calls/use-live";
import type { Mailbox } from "@/components/email/types";
import { assigneeLabel, draftStatusTone, priorityTone } from "./logic";
import { MessageView } from "./MessageView";
import type { Draft, ThreadDetail, Thread } from "./types";

const THREAD_STATUSES = ["open", "awaiting_review", "replied", "closed"] as const;
const DRAFT_STATUSES = ["pending_review", "queued", "generating", "approved", "sending", "sent", "send_failed", "rejected", "generation_failed"] as const;

function useMailboxes() {
  const can = useCan();
  return useApi<Page<Mailbox>>(can("emails:campaigns:manage") ? "/api/v1/email/mailboxes?limit=100" : null);
}

export function ThreadList() {
  const session = useSession();
  const [status, setStatus] = React.useState("");
  const [mailbox, setMailbox] = React.useState("");
  const [mine, setMine] = React.useState(false);
  const mailboxes = useMailboxes();
  const list = useCursorList<Thread>(withQuery("/api/v1/email/inbox/threads", { status, mailbox_id: mailbox, assigned_to_me: mine || undefined }));
  const hint = useLiveHint({ events: ["email.draft.updated"] });
  const columns: Column<Thread>[] = [
    {
      key: "subject",
      header: "Subject",
      cell: (t) => (
        <Link href={`/app/inbox/threads/${t.id}`} className="block min-w-0 text-brand hover:underline">
          <span className="line-clamp-1 font-medium">{t.subject || "(no subject)"}</span>
          <span className="text-xs text-muted">{t.message_count} message{t.message_count === 1 ? "" : "s"}{t.intent ? ` · ${t.intent.replace(/_/g, " ")}` : ""}</span>
        </Link>
      ),
    },
    { key: "status", header: "Status", cell: (t) => <Badge tone={t.status === "awaiting_review" ? "warn" : t.status === "replied" ? "ok" : "neutral"}>{t.status.replace(/_/g, " ")}</Badge> },
    { key: "priority", header: "Priority", cell: (t) => <Badge tone={priorityTone(t.priority)}>{t.priority}</Badge>, hideOnMobile: true },
    { key: "assigned", header: "Assigned", cell: (t) => assigneeLabel(t.assigned_to, session.user.id), hideOnMobile: true },
    { key: "last", header: "Last message", cell: (t) => <span title={formatDateTime(t.last_message_at)}>{relativeTime(t.last_message_at)}</span> },
  ];
  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="th-status" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select id="th-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-44">
            <option value="">All</option>
            {THREAD_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </Select>
        </div>
        {mailboxes.data && mailboxes.data.items.length > 1 ? (
          <div>
            <label htmlFor="th-mailbox" className="mb-1 block text-xs text-muted">
              Mailbox
            </label>
            <Select id="th-mailbox" value={mailbox} onChange={(e) => setMailbox(e.target.value)} className="w-56">
              <option value="">All mailboxes</option>
              {mailboxes.data.items.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.address}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
        <Checkbox checked={mine} onChange={(e) => setMine(e.target.checked)} label="Assigned to me" className="pb-2" />
        {hint.stale ? (
          <Button size="sm" variant="secondary" onClick={() => { hint.clear(); list.reload(); }}>
            New activity — refresh
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Inbox threads"
        columns={columns}
        rows={list.items}
        rowKey={(t) => t.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={status || mine || mailbox ? "No threads match" : "The inbox is empty"} description="Replies to your inbound mailboxes appear here." />}
      />
    </Card>
  );
}

/**
 * Assignment goes through the draft (it updates the thread as well). Without user-management rights only
 * "assign to me" and "unassign" are offered; the server checks the assignee can review drafts.
 */
export function AssignControl({ draftId, assignedTo, onDone, disabled }: { draftId: string; assignedTo: string | null; onDone: () => void; disabled?: boolean }) {
  const can = useCan();
  const session = useSession();
  const toast = useToast();
  const members = useApi<Page<Membership>>(can("tenant:users:manage") ? "/api/v1/memberships?limit=200" : null);
  const [busy, setBusy] = React.useState(false);
  async function assign(assignee: string | null) {
    setBusy(true);
    try {
      await api(`/api/v1/email/drafts/${draftId}/assign`, { method: "POST", body: { assignee } });
      toast({ tone: "success", title: assignee ? (assignee === session.user.id ? "Assigned to you" : "Assigned") : "Unassigned" });
      onDone();
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Could not assign", description: e.message });
    } finally {
      setBusy(false);
    }
  }
  if (!can("emails:drafts:review")) return <span className="text-sm">{assigneeLabel(assignedTo, session.user.id)}</span>;
  const active = (members.data?.items ?? []).filter((m) => m.status === "active");
  return (
    <div className="flex flex-wrap items-center gap-2">
      {active.length > 0 ? (
        <>
          <label htmlFor={`assign-${draftId}`} className="sr-only">
            Assignee
          </label>
          <Select id={`assign-${draftId}`} value={assignedTo ?? ""} disabled={busy || disabled} onChange={(e) => assign(e.target.value || null)} className="w-56">
            <option value="">Unassigned</option>
            {assignedTo && !active.some((m) => m.user_id === assignedTo) ? <option value={assignedTo}>{assigneeLabel(assignedTo, session.user.id)}</option> : null}
            {active.map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {m.display_name ?? m.email}
                {m.user_id === session.user.id ? " (you)" : ""}
              </option>
            ))}
          </Select>
        </>
      ) : (
        <>
          <span className="text-sm">{assigneeLabel(assignedTo, session.user.id)}</span>
          {assignedTo !== session.user.id ? (
            <Button size="sm" variant="secondary" disabled={busy || disabled} onClick={() => assign(session.user.id)}>
              Assign to me
            </Button>
          ) : null}
          {assignedTo ? (
            <Button size="sm" variant="ghost" disabled={busy || disabled} onClick={() => assign(null)}>
              Unassign
            </Button>
          ) : null}
        </>
      )}
    </div>
  );
}

export function ThreadView({ threadId }: { threadId: string }) {
  const can = useCan();
  const thread = useApi<ThreadDetail>(`/api/v1/email/inbox/threads/${threadId}`);
  const draftIds = (thread.data?.drafts ?? []).map((d) => d.id);
  useLiveRefresh(thread.reload, { events: ["email.draft.updated"], match: (e) => e.type === "ui" && draftIds.includes(e.data.draft_id ?? ""), pollMs: 0 });
  if (thread.loading && !thread.data) return <Card><LoadingState label="Loading thread" /></Card>;
  if (thread.error && !thread.data) return <Card><ErrorState problem={thread.error.problem} onRetry={thread.reload} /></Card>;
  const t = thread.data;
  if (!t) return null;
  const latestDraft = [...t.drafts].reverse()[0];
  return (
    <>
      <PageHeader title={t.subject || "(no subject)"} description={`${t.message_count} messages · last ${relativeTime(t.last_message_at)} · ${t.status.replace(/_/g, " ")}`} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="flex flex-col gap-3">
          {t.messages.length === 0 ? <Card><EmptyState title="No messages in this thread" /></Card> : t.messages.map((m) => <MessageView key={m.id} message={m} />)}
        </div>
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader title="Thread" />
            <div className="flex flex-col gap-3 px-5 py-4 text-sm">
              <div className="flex flex-wrap gap-1.5">
                <Badge tone={priorityTone(t.priority)}>{t.priority} priority</Badge>
                {t.intent ? <Badge>{t.intent.replace(/_/g, " ")}</Badge> : null}
              </div>
              <div>
                <p className="mb-1 text-xs text-muted">Assigned to</p>
                {latestDraft ? <AssignControl draftId={latestDraft.id} assignedTo={t.assigned_to} onDone={thread.reload} /> : <p>{t.assigned_to ? `${t.assigned_to.slice(0, 8)}…` : "Unassigned"} <span className="block text-xs text-muted">Assignment is available once a reply draft exists.</span></p>}
              </div>
              {t.contact_id && can("leads:read") ? (
                <Link className="text-brand hover:underline" href={`/app/crm/contacts/${t.contact_id}`}>
                  Open contact
                </Link>
              ) : null}
            </div>
          </Card>
          <Card>
            <CardHeader title="Reply drafts" description="AI-written drafts are never sent without a person approving them." />
            {t.drafts.length === 0 ? (
              <p className="px-5 py-4 text-sm text-muted">No reply draft for this thread.</p>
            ) : (
              <ul className="divide-y divide-border">
                {t.drafts.map((d) => (
                  <li key={d.id} className="flex items-center justify-between gap-2 px-5 py-3 text-sm">
                    <span className="inline-flex items-center gap-2">
                      <Bot className="h-4 w-4 text-brand" aria-hidden />
                      <Badge tone={draftStatusTone(d.status)}>{d.status.replace(/_/g, " ")}</Badge>
                      {d.escalated ? <Badge tone="danger">escalated</Badge> : null}
                    </span>
                    {can("emails:drafts:review") ? (
                      <Link className="text-brand hover:underline" href={`/app/inbox/drafts/${d.id}`}>
                        Review v{d.current_version}
                      </Link>
                    ) : (
                      <span className="text-xs text-muted">v{d.current_version}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

export function DraftQueue() {
  const session = useSession();
  const [status, setStatus] = React.useState("pending_review");
  const [escalated, setEscalated] = React.useState("");
  const [mine, setMine] = React.useState(false);
  const list = useCursorList<Draft>(withQuery("/api/v1/email/drafts", { status, escalated, assigned_to_me: mine || undefined }));
  const hint = useLiveHint({ events: ["email.draft.updated"] });
  const columns: Column<Draft>[] = [
    {
      key: "subject",
      header: "Reply to",
      cell: (d) => (
        <Link href={`/app/inbox/drafts/${d.id}`} className="block min-w-0 text-brand hover:underline">
          <span className="line-clamp-1 font-medium">{d.subject || "(no subject)"}</span>
          <span className="text-xs text-muted">v{d.current_version}{d.intent ? ` · ${d.intent.replace(/_/g, " ")}` : ""}</span>
        </Link>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (d) => (
        <div className="flex flex-wrap gap-1">
          <Badge tone={draftStatusTone(d.status)}>{d.status.replace(/_/g, " ")}</Badge>
          {d.escalated ? <Badge tone="danger">escalated</Badge> : null}
          {d.sensitive ? <Badge tone="danger">sensitive</Badge> : null}
        </div>
      ),
    },
    { key: "priority", header: "Priority", cell: (d) => <Badge tone={priorityTone(d.priority)}>{d.priority}</Badge>, hideOnMobile: true },
    { key: "assigned", header: "Assigned", cell: (d) => assigneeLabel(d.assigned_to, session.user.id), hideOnMobile: true },
    { key: "created", header: "Waiting since", cell: (d) => <span title={formatDateTime(d.created_at)}>{relativeTime(d.created_at)}</span> },
  ];
  return (
    <Card>
      <CardHeader title="Approval queue" description="AI reply drafts waiting for a person. Open one to compare it with the original, edit, and approve or reject." />
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="dq-status" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select id="dq-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-44">
            <option value="">All</option>
            {DRAFT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label htmlFor="dq-esc" className="mb-1 block text-xs text-muted">
            Escalation
          </label>
          <Select id="dq-esc" value={escalated} onChange={(e) => setEscalated(e.target.value)} className="w-40">
            <option value="">Any</option>
            <option value="true">Escalated</option>
            <option value="false">Not escalated</option>
          </Select>
        </div>
        <Checkbox checked={mine} onChange={(e) => setMine(e.target.checked)} label="Assigned to me" className="pb-2" />
        {hint.stale ? (
          <Button size="sm" variant="secondary" onClick={() => { hint.clear(); list.reload(); }}>
            New drafts — refresh
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="AI reply drafts"
        columns={columns}
        rows={list.items}
        rowKey={(d) => d.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={status === "pending_review" ? "Nothing waiting for review" : "No drafts match"} description="Drafts appear when an AI agent writes a reply to an incoming email." />}
      />
    </Card>
  );
}

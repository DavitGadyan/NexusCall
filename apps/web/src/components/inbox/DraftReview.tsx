"use client";
/**
 * AI reply draft review (build.md §4.2). The original message sits next to the draft; the draft is styled
 * and labelled as AI-written and not sent. Review actions (edit = new version, regenerate, reject, assign,
 * escalate) never send. "Approve and send" is the only path to sending: it needs `emails:drafts:approve`,
 * a recent MFA sign-in (server-enforced), an explicit confirmation that shows the recipient and the exact
 * text, an Idempotency-Key, and it names the exact `{version}` approved (409 if a newer one exists).
 */
import { AlertTriangle, BookOpen, Bot, PenLine, Send, ShieldCheck, UserRound } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Page } from "@/lib/types";
import { useCan } from "@/components/shell/session-context";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  ConfirmDialog,
  ConflictNotice,
  Dialog,
  ErrorState,
  Field,
  FormError,
  LoadingState,
  Notice,
  PageHeader,
  Select,
  Textarea,
  useRecentStepUp,
  useToast,
} from "@/components/ui";
import { useIdempotencyKey, useLiveHint } from "@/components/calls/use-live";
import type { Mailbox } from "@/components/email/types";
import { AssignControl } from "./Inbox";
import { actionLabel, approveBlockReason, canEdit, canEscalate, canReject, confidence, currentVersion, draftFlags, draftStatusTone, type FlagInfo, priorityTone, replySubject } from "./logic";
import { MessageView } from "./MessageView";
import { type ApprovalOut, type DraftDetail, type DraftVersion, ESCALATE_REASONS, REJECT_REASONS } from "./types";

const SENT_OR_SENDING = ["approved", "sending", "sent"];

export function DraftReview({ draftId }: { draftId: string }) {
  const can = useCan();
  const toast = useToast();
  const res = useApi<DraftDetail>(`/api/v1/email/drafts/${draftId}`);
  const [viewing, setViewing] = React.useState<number | null>(null);
  const [editing, setEditing] = React.useState(false);
  const [text, setText] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [editError, setEditError] = React.useState<ApiError | null>(null);
  const [approveOpen, setApproveOpen] = React.useState(false);
  const [reasonDialog, setReasonDialog] = React.useState<"reject" | "escalate" | null>(null);
  const [regenOpen, setRegenOpen] = React.useState(false);
  const [retryOpen, setRetryOpen] = React.useState(false);
  const [showThread, setShowThread] = React.useState(false);

  const hint = useLiveHint({ events: ["email.draft.updated"], match: (e) => e.type === "ui" && e.data.draft_id === draftId });
  const busyElsewhere = editing || approveOpen || reasonDialog !== null || regenOpen || retryOpen;
  React.useEffect(() => {
    // Auto-refresh unless the reviewer is in the middle of something; then show a banner instead.
    if (hint.stale && !busyElsewhere) {
      hint.clear();
      res.reload();
    }
  }, [hint.stale, busyElsewhere]); // eslint-disable-line react-hooks/exhaustive-deps
  const generating = res.data?.status === "queued" || res.data?.status === "generating" || res.data?.status === "sending";
  React.useEffect(() => {
    if (!generating || busyElsewhere) return;
    const t = setInterval(() => res.reload(), 5000);
    return () => clearInterval(t);
  }, [generating, busyElsewhere]); // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => {
    setViewing(null);
  }, [res.data?.current_version]);

  if (res.loading && !res.data) return <Card><LoadingState label="Loading draft" /></Card>;
  if (res.error && !res.data) return <Card><ErrorState problem={res.error.problem} onRetry={res.reload} /></Card>;
  const d = res.data;
  if (!d) return null;

  const current = currentVersion(d);
  const shown = (viewing !== null ? d.versions.find((v) => v.version === viewing) : null) ?? current;
  const flags = draftFlags(current, { sensitive: d.sensitive });
  const blocking = approveBlockReason(d, current?.version ?? 0, { unsavedEdits: editing });
  const mayReview = can("emails:drafts:review");
  const mayApprove = can("emails:drafts:approve");
  const lastCommand = [...d.send_commands].sort((a, b) => b.version - a.version)[0];
  const sent = d.status === "sent";
  const others = d.thread_messages.filter((m) => m.id !== d.original_message.id);

  async function saveEdit() {
    if (!d) return;
    setSaving(true);
    setEditError(null);
    try {
      await api(`/api/v1/email/drafts/${draftId}`, { method: "PUT", body: { expected_version: d.current_version, text } });
      toast({ tone: "success", title: `Saved as version ${d.current_version + 1}`, description: "It needs a fresh approval before it can be sent." });
      setEditing(false);
      res.reload();
    } catch (err) {
      setEditError(toApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHeader
        title={replySubject(d.subject || "(no subject)")}
        description={`Draft reply · created ${relativeTime(d.created_at)} · ${d.versions.length} version${d.versions.length === 1 ? "" : "s"}`}
        actions={
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={draftStatusTone(d.status)}>{d.status.replace(/_/g, " ")}</Badge>
            <Badge tone={priorityTone(d.priority)}>{d.priority}</Badge>
            {d.intent ? <Badge>{d.intent.replace(/_/g, " ")}</Badge> : null}
          </div>
        }
      />
      <div className="mb-4 flex flex-col gap-3">
        {hint.stale ? (
          <ConflictNotice onReload={() => { hint.clear(); setEditing(false); res.reload(); }}>
            This draft changed while you were working on it (for example a new version or someone else&apos;s decision). Reload before you continue.
          </ConflictNotice>
        ) : null}
        {d.escalated ? (
          <Notice tone="danger" title="Escalated">
            {d.escalation_reason ? `Reason: ${d.escalation_reason.replace(/^(intent_|flag_)/, "").replace(/_/g, " ")}.` : null} Handle with extra care; a lead may need to decide.
          </Notice>
        ) : null}
        {d.status === "generating" || d.status === "queued" ? <Notice tone="info" title="The AI is writing a draft">This page refreshes automatically.</Notice> : null}
        {d.status === "generation_failed" ? <Notice tone="danger" title="Drafting failed">{d.last_error ?? "The AI could not write a draft."} You can write the reply yourself (Edit) or regenerate.</Notice> : null}
        {d.status === "send_failed" ? <Notice tone="danger" title="Sending failed">{lastCommand?.last_error ?? d.last_error ?? "The approved reply could not be sent."}</Notice> : null}
        {SENT_OR_SENDING.includes(d.status) && lastCommand ? (
          <Notice tone={sent ? "ok" : "info"} title={sent ? `Version ${lastCommand.version} was sent` : `Version ${lastCommand.version} is approved and queued for sending`}>
            To {lastCommand.to_address} · approved {formatDateTime(lastCommand.approved_at)}
            {lastCommand.sent_at ? ` · sent ${formatDateTime(lastCommand.sent_at)}` : ""}
          </Notice>
        ) : null}
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <section aria-labelledby="orig-heading" className="flex min-w-0 flex-col gap-3">
          <h2 id="orig-heading" className="inline-flex items-center gap-2 text-sm font-semibold">
            <UserRound className="h-4 w-4 text-muted" aria-hidden /> Original message
          </h2>
          <MessageView message={d.original_message} />
          {d.summary ? (
            <Card className="px-4 py-3 text-sm">
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">AI summary of the request</p>
              <p className="whitespace-pre-wrap">{d.summary}</p>
            </Card>
          ) : null}
          {others.length > 0 ? (
            <div>
              <Button size="sm" variant="ghost" aria-expanded={showThread} onClick={() => setShowThread((v) => !v)}>
                {showThread ? "Hide" : "Show"} {others.length} other message{others.length === 1 ? "" : "s"} in this thread
              </Button>
              {showThread ? (
                <div className="mt-2 flex flex-col gap-2">
                  {others.map((m) => (
                    <MessageView key={m.id} message={m} compact />
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </section>
        <section aria-labelledby="draft-heading" className="flex min-w-0 flex-col gap-3">
          <h2 id="draft-heading" className="inline-flex items-center gap-2 text-sm font-semibold">
            <Bot className="h-4 w-4 text-brand" aria-hidden /> Reply draft
          </h2>
          <div className="rounded-lg border-2 border-dashed border-brand/50 bg-brand/[0.04]" data-testid="ai-draft">
            <DraftBanner version={shown} status={d.status} isCurrent={shown?.version === d.current_version} approvedVersion={d.approved_version} />
            {flags.length > 0 && shown?.version === d.current_version ? <FlagList flags={flags} /> : null}
            <div className="px-4 py-3">
              {editing ? (
                <div className="flex flex-col gap-2">
                  {editError?.isConflict ? (
                    <ConflictNotice onReload={() => { setEditing(false); setEditError(null); res.reload(); }}>
                      A newer version was saved meanwhile. Copy your text if you need it, then reload.
                    </ConflictNotice>
                  ) : null}
                  <Field id="draft-text" label="Reply text" hint="Plain text. Saving creates a new version written by you." error={editError?.fields.text}>
                    {(p) => <Textarea {...p} rows={16} maxLength={20000} value={text} onChange={(e) => setText(e.target.value)} />}
                  </Field>
                  <FormError message={editError && !editError.isConflict && !editError.isStepUp && Object.keys(editError.fields).length === 0 ? editError.message : null} />
                  <div className="flex justify-end gap-2">
                    <Button variant="secondary" onClick={() => { setEditing(false); setEditError(null); }} disabled={saving}>
                      Discard
                    </Button>
                    <Button onClick={saveEdit} disabled={saving || !text.trim() || text === (current?.body_text ?? "")}>
                      {saving ? "Saving…" : "Save as new version"}
                    </Button>
                  </div>
                </div>
              ) : shown ? (
                <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed" data-testid="draft-body">{shown.body_text}</pre>
              ) : (
                <p className="text-sm text-muted">No draft text yet.</p>
              )}
            </div>
            {shown && !editing ? <Provenance version={shown} /> : null}
          </div>
          {mayReview && !sent ? (
            <div className="flex flex-wrap items-center gap-2" aria-label="Review actions" role="group">
              {canEdit(d.status) && !editing ? (
                <Button variant="secondary" size="sm" onClick={() => { setText(current?.body_text ?? ""); setEditError(null); setEditing(true); }}>
                  <PenLine className="h-4 w-4" aria-hidden /> Edit
                </Button>
              ) : null}
              {canEdit(d.status) ? (
                <Button variant="secondary" size="sm" onClick={() => setRegenOpen(true)} disabled={editing}>
                  Regenerate
                </Button>
              ) : null}
              {canEscalate(d.status) && !d.escalated ? (
                <Button variant="secondary" size="sm" onClick={() => setReasonDialog("escalate")} disabled={editing}>
                  Escalate
                </Button>
              ) : null}
              {canReject(d.status) ? (
                <Button variant="ghost" size="sm" onClick={() => setReasonDialog("reject")} disabled={editing}>
                  Reject
                </Button>
              ) : null}
              <span className="ml-auto" />
              {mayApprove && d.status === "send_failed" && d.approved_version ? (
                <Button size="sm" variant="secondary" onClick={() => setRetryOpen(true)}>
                  Retry sending v{d.approved_version}
                </Button>
              ) : null}
              {mayApprove && (d.status === "pending_review" || editing) ? (
                <span className="inline-flex flex-col items-end">
                  <Button onClick={() => setApproveOpen(true)} disabled={blocking !== null} data-testid="open-approve">
                    <Send className="h-4 w-4" aria-hidden /> Approve and send…
                  </Button>
                  {blocking ? <span className="mt-1 text-xs text-muted">{blocking}</span> : null}
                </span>
              ) : null}
            </div>
          ) : null}
          {!mayApprove && d.status === "pending_review" ? <p className="text-xs text-muted">You can review and edit this draft. Someone with approval rights must approve it before it is sent.</p> : null}
          <Card>
            <CardHeader title="Assignment" />
            <div className="px-5 py-3">
              <AssignControl draftId={d.id} assignedTo={d.assigned_to} onDone={res.reload} disabled={editing} />
            </div>
          </Card>
          <VersionHistory draft={d} viewing={shown?.version ?? null} onView={setViewing} />
          <ActivityLog draft={d} />
        </section>
      </div>
      {current ? (
        <ApproveDialog
          open={approveOpen}
          draft={d}
          version={current}
          flags={flags}
          onClose={() => setApproveOpen(false)}
          onDone={() => {
            setApproveOpen(false);
            res.reload();
          }}
        />
      ) : null}
      <ReasonDialog
        mode={reasonDialog}
        onClose={() => setReasonDialog(null)}
        onSubmit={async (reason) => {
          if (!reasonDialog) return;
          await api(`/api/v1/email/drafts/${draftId}/${reasonDialog}`, { method: "POST", body: { reason } });
          toast({ tone: "success", title: reasonDialog === "reject" ? "Draft rejected" : "Draft escalated", description: reasonDialog === "reject" ? "Nothing was sent. The thread is open again." : "Approvers are notified." });
          res.reload();
        }}
      />
      <ConfirmDialog
        open={regenOpen}
        onClose={() => setRegenOpen(false)}
        title="Ask the AI for a new draft?"
        description="The current text stays in the version history. Any approval is withdrawn; the new draft needs review again."
        confirmLabel="Regenerate"
        tone="primary"
        onConfirm={async () => {
          await api(`/api/v1/email/drafts/${draftId}/regenerate`, { method: "POST" });
          toast({ tone: "info", title: "Regenerating", description: "The new draft appears here when it is ready." });
          res.reload();
        }}
      />
      {d.approved_version ? (
        <RetrySendDialog
          open={retryOpen}
          draft={d}
          onClose={() => setRetryOpen(false)}
          onDone={() => {
            setRetryOpen(false);
            res.reload();
          }}
        />
      ) : null}
    </>
  );
}

function DraftBanner({ version, status, isCurrent, approvedVersion }: { version: DraftVersion | null; status: string; isCurrent: boolean; approvedVersion: number | null }) {
  const ai = version?.author_type === "ai";
  const sent = status === "sent" && version?.version === approvedVersion;
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-2 rounded-t-md border-b px-4 py-2 text-sm", ai ? "border-brand/30 bg-brand/10" : "border-border bg-surface-2")}>
      <p className="inline-flex items-center gap-2 font-medium">
        {ai ? <Bot className="h-4 w-4 text-brand" aria-hidden /> : <PenLine className="h-4 w-4" aria-hidden />}
        {ai ? "AI-written draft" : "Edited by a person"}
        <span className="text-muted">·</span>
        <span className={sent ? "text-ok" : "text-warn"}>{sent ? "Sent" : "Not sent"}</span>
      </p>
      <span className="text-xs text-muted">
        Version {version?.version ?? "—"}
        {isCurrent ? " (latest)" : " (older version)"}
        {approvedVersion && version?.version === approvedVersion ? " · approved" : ""}
      </span>
    </div>
  );
}

function FlagList({ flags }: { flags: FlagInfo[] }) {
  return (
    <ul className="flex flex-col gap-1.5 border-b border-border px-4 py-2.5" aria-label="Quality checks">
      {flags.map((f) => (
        <li key={f.flag} className="flex items-start gap-2 text-sm">
          <AlertTriangle className={cn("mt-0.5 h-4 w-4 shrink-0", f.tone === "danger" ? "text-danger" : "text-warn")} aria-hidden />
          <span>
            <Badge tone={f.tone}>{f.label}</Badge> <span className="text-fg/80">{f.description}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function Provenance({ version }: { version: DraftVersion }) {
  const conf = confidence(version);
  return (
    <div className="flex flex-col gap-2 border-t border-border px-4 py-3 text-sm">
      <div>
        <p className="mb-1 inline-flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
          <BookOpen className="h-3.5 w-3.5" aria-hidden /> Cited knowledge sources
        </p>
        {version.citations.length === 0 ? (
          <p className={cn("text-sm", version.ai_generated ? "text-warn" : "text-muted")}>{version.ai_generated ? "None. Nothing in this reply is backed by your knowledge base." : "Not applicable to a human-written version."}</p>
        ) : (
          <ol className="flex list-decimal flex-col gap-0.5 pl-5" data-testid="citations">
            {version.citations.map((c) => (
              <li key={`${c.source_id}-${c.chunk_id}`}>
                {c.source_name} <span className="font-mono text-[11px] text-muted">passage {c.chunk_id.slice(0, 8)}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
      <p className="text-xs text-muted">
        {version.ai_generated
          ? `Written by ${version.provider ?? "AI"}${version.model ? ` ${version.model}` : ""}${version.prompt_version ? ` · prompt v${version.prompt_version}` : ""}${conf !== null ? ` · confidence ${Math.round(conf * 100)}%` : ""}`
          : `Written by a person (${version.author_id.slice(0, 8)}…)`}{" "}
        · {formatDateTime(version.created_at)}
      </p>
    </div>
  );
}

function VersionHistory({ draft, viewing, onView }: { draft: DraftDetail; viewing: number | null; onView: (v: number) => void }) {
  const versions = [...draft.versions].sort((a, b) => b.version - a.version);
  if (versions.length === 0) return null;
  return (
    <Card>
      <CardHeader title="Version history" description="Every version is kept. Only the latest can be approved." />
      <ul className="divide-y divide-border">
        {versions.map((v) => (
          <li key={v.version}>
            <button
              type="button"
              onClick={() => onView(v.version)}
              aria-pressed={viewing === v.version}
              className={cn("flex w-full items-center justify-between gap-2 px-5 py-2 text-left text-sm hover:bg-surface-2", viewing === v.version && "bg-surface-2")}
            >
              <span className="inline-flex items-center gap-2">
                {v.author_type === "ai" ? <Bot className="h-4 w-4 text-brand" aria-label="AI" /> : <PenLine className="h-4 w-4" aria-label="Person" />}
                v{v.version}
                {v.version === draft.current_version ? <Badge tone="brand">latest</Badge> : null}
                {v.version === draft.approved_version ? <Badge tone="ok">approved</Badge> : null}
              </span>
              <span className="text-xs text-muted">{relativeTime(v.created_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function ActivityLog({ draft }: { draft: DraftDetail }) {
  if (draft.actions.length === 0) return null;
  return (
    <Card>
      <CardHeader title="Review history" description="Recorded for audit; it cannot be changed." />
      <ol className="flex flex-col px-5 py-3">
        {[...draft.actions].reverse().map((a, i) => (
          <li key={`${a.created_at}-${i}`} className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border py-1.5 text-sm last:border-b-0">
            <span>
              {actionLabel(a.action)}
              {a.version ? <span className="text-muted"> · v{a.version}</span> : null}
              {a.reason ? <span className="text-muted"> · {a.reason.replace(/_/g, " ")}</span> : null}
              <span className="block text-xs text-muted">
                {a.actor_type === "user" ? "Person" : a.actor_type} {a.actor_id.slice(0, 8)}
              </span>
            </span>
            <time className="text-xs text-muted" dateTime={a.created_at}>
              {formatDateTime(a.created_at)}
            </time>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function ApproveDialog({ open, draft, version, flags, onClose, onDone }: { open: boolean; draft: DraftDetail; version: DraftVersion; flags: FlagInfo[]; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const can = useCan();
  const stepped = useRecentStepUp();
  const key = useIdempotencyKey(open);
  const mailboxes = useApi<Page<Mailbox>>(open && can("emails:campaigns:manage") ? "/api/v1/email/mailboxes?limit=100" : null);
  const [checked, setChecked] = React.useState(false);
  const [flagsChecked, setFlagsChecked] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setChecked(false);
      setFlagsChecked(false);
      setError(null);
    }
  }, [open]);
  const serious = flags.filter((f) => f.tone === "danger");
  const from = mailboxes.data?.items.find((m) => m.id === draft.mailbox_id)?.address;
  const ready = checked && (serious.length === 0 || flagsChecked);

  async function approve() {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await api<ApprovalOut>(`/api/v1/email/drafts/${draft.id}/approve`, { method: "POST", body: { version: version.version }, idempotencyKey: key || true });
      toast({ tone: "success", title: `Version ${version.version} approved`, description: `It is queued for sending to ${draft.original_message.from_address}.` });
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={busy}
      size="xl"
      title="Approve and send this reply"
      description="This is the exact message that will be sent. After approval the text is frozen; any later edit needs a new approval."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={approve} disabled={busy || !ready || error?.isConflict} data-testid="confirm-approve">
            <ShieldCheck className="h-4 w-4" aria-hidden /> {busy ? "Approving…" : `Approve v${version.version} and send`}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {error?.isConflict ? (
          <ConflictNotice onReload={onDone}>A newer version exists or the draft changed (for example someone else approved or edited it). Reload to review the latest version.</ConflictNotice>
        ) : null}
        <dl className="grid grid-cols-[80px_1fr] gap-x-4 gap-y-1.5 rounded-md border border-border bg-surface-2 px-4 py-3 text-sm">
          <dt className="text-muted">To</dt>
          <dd className="break-all font-medium" data-testid="approve-to">
            {draft.original_message.from_name ? `${draft.original_message.from_name} ` : ""}&lt;{draft.original_message.from_address}&gt;
          </dd>
          {from ? (
            <>
              <dt className="text-muted">From</dt>
              <dd className="break-all">{from}</dd>
            </>
          ) : null}
          <dt className="text-muted">Subject</dt>
          <dd className="break-words">{replySubject(draft.subject || draft.original_message.subject)}</dd>
          <dt className="text-muted">Version</dt>
          <dd>
            v{version.version} · {version.author_type === "ai" ? "AI-written" : "edited by a person"}
          </dd>
        </dl>
        <div>
          <p className="mb-1 text-sm font-medium">Exact text</p>
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface p-3 font-sans text-sm" data-testid="approve-text">
            {version.body_text}
          </pre>
        </div>
        {serious.length > 0 ? (
          <Notice tone="danger" title="Quality checks raised concerns">
            <ul className="list-disc pl-5">
              {serious.map((f) => (
                <li key={f.flag}>
                  <span className="font-medium">{f.label}:</span> {f.description}
                </li>
              ))}
            </ul>
          </Notice>
        ) : null}
        {version.ai_generated && version.citations.length === 0 ? <Notice tone="warn">This AI-written reply cites no knowledge sources.</Notice> : null}
        {!stepped ? (
          <Notice tone="info" title="Multi-factor check">
            Approving needs a recent MFA sign-in. If yours is older than five minutes you will be asked to verify, then approve again.
          </Notice>
        ) : null}
        <Checkbox checked={checked} onChange={(e) => setChecked(e.target.checked)} label="I checked the recipient and the exact text above, and I want this reply sent." data-testid="approve-confirm-check" />
        {serious.length > 0 ? <Checkbox checked={flagsChecked} onChange={(e) => setFlagsChecked(e.target.checked)} label="I reviewed the flagged concerns and the reply is correct." /> : null}
        <FormError message={error && !error.isConflict && !error.isStepUp ? error.message : null} />
      </div>
    </Dialog>
  );
}

function RetrySendDialog({ open, draft, onClose, onDone }: { open: boolean; draft: DraftDetail; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const key = useIdempotencyKey(open);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const approved = draft.versions.find((v) => v.version === draft.approved_version);
  React.useEffect(() => {
    if (open) setError(null);
  }, [open]);
  async function retry() {
    setBusy(true);
    setError(null);
    try {
      await api<ApprovalOut>(`/api/v1/email/drafts/${draft.id}/send`, { method: "POST", body: { version: draft.approved_version }, idempotencyKey: key || true });
      toast({ tone: "success", title: "Send retried", description: "The already-approved version is queued again." });
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={busy}
      size="lg"
      title={`Retry sending approved version ${draft.approved_version}`}
      description="Only the version that was already approved is re-queued. Nothing new is approved here."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={retry} disabled={busy || error?.isConflict}>
            {busy ? "Retrying…" : "Retry send"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        {error?.isConflict ? <ConflictNotice onReload={onDone} /> : null}
        <p>
          To <span className="font-medium">{draft.original_message.from_address}</span>
        </p>
        {approved ? <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-3 font-sans">{approved.body_text}</pre> : null}
        <FormError message={error && !error.isConflict && !error.isStepUp ? error.message : null} />
      </div>
    </Dialog>
  );
}

function ReasonDialog({ mode, onClose, onSubmit }: { mode: "reject" | "escalate" | null; onClose: () => void; onSubmit: (reason: string) => Promise<void> }) {
  const reasons: readonly string[] = mode === "escalate" ? ESCALATE_REASONS : REJECT_REASONS;
  const [reason, setReason] = React.useState<string>(reasons[0] ?? "other");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (mode) {
      setReason((mode === "escalate" ? ESCALATE_REASONS : REJECT_REASONS)[0]);
      setError(null);
    }
  }, [mode]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit(reason);
      onClose();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={mode !== null}
      onClose={onClose}
      busy={busy}
      size="sm"
      title={mode === "escalate" ? "Escalate this draft" : "Reject this draft"}
      description={mode === "escalate" ? "Approvers are notified. The draft stays in the queue." : "Nothing is sent. The thread goes back to open."}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="draft-reason" label="Reason">
          {(p) => (
            <Select {...p} value={reason} onChange={(e) => setReason(e.target.value)}>
              {reasons.map((r) => (
                <option key={r} value={r}>
                  {r.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {error?.isConflict ? <Notice tone="warn">{error.message}. Reload the draft to see its current state.</Notice> : <FormError message={error && !error.isStepUp ? error.message : null} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant={mode === "reject" ? "danger" : "primary"} disabled={busy}>
            {busy ? "Working…" : mode === "escalate" ? "Escalate" : "Reject draft"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

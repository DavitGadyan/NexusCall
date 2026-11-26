"use client";
/**
 * Email campaigns: list, builder (template, segment, sender, window, pacing), lifecycle actions and the
 * delivery report. Launch and schedule carry an Idempotency-Key. Only senders on verified domains can send.
 * The report shows provider outcomes only; opens and clicks are not tracked.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { type ApiState, useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Page } from "@/lib/types";
import { cn } from "@/lib/cn";
import { useCan } from "@/components/shell/session-context";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  ConflictNotice,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  Input,
  KeyValues,
  LoadingState,
  Notice,
  PageHeader,
  Select,
  Stat,
  useToast,
} from "@/components/ui";
import { toLocalInput } from "@/components/bookings/calendar";
import { useIdempotencyKey } from "@/components/calls/use-live";
import { campaignActions, campaignTone, isCampaignLive, percent, reportRates, reportRows, reportSegments, scheduleBody, senderBlockReason } from "./logic";
import type { Campaign, OutboundMessage, Report, Segment, Sender, Template } from "./types";

const STATES = ["draft", "scheduled", "running", "paused", "completed", "canceled", "failed"] as const;

export function CampaignList() {
  const can = useCan();
  const [state, setState] = React.useState("");
  const list = useCursorList<Campaign>(withQuery("/api/v1/email/campaigns", { state }));
  const columns: Column<Campaign>[] = [
    {
      key: "name",
      header: "Campaign",
      cell: (c) => (
        <Link href={`/app/campaigns/email/${c.id}`} className="font-medium text-brand hover:underline">
          {c.name}
        </Link>
      ),
    },
    { key: "state", header: "State", cell: (c) => <Badge tone={campaignTone(c.state)}>{c.state}</Badge> },
    { key: "start", header: "Starts", cell: (c) => (c.scheduled_at ? formatDateTime(c.scheduled_at) : "—"), hideOnMobile: true },
    { key: "pace", header: "Pace", cell: (c) => <span className="tabular">{c.max_per_minute}/min</span>, hideOnMobile: true },
    { key: "updated", header: "Updated", cell: (c) => relativeTime(c.updated_at), hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader
        title="Email campaigns"
        description="Sent only to contacts with email consent who are not suppressed, inside the workspace's email window."
        actions={
          can("emails:campaigns:manage") ? (
            <Link href="/app/campaigns/email/new" className="inline-flex h-8 items-center rounded-md bg-brand px-3 text-sm font-medium text-brand-fg hover:bg-brand/90" data-testid="new-campaign">
              New campaign
            </Link>
          ) : null
        }
      />
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="ec-state" className="mb-1 block text-xs text-muted">
            State
          </label>
          <Select id="ec-state" value={state} onChange={(e) => setState(e.target.value)} className="w-40">
            <option value="">All</option>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <DataTable
        caption="Email campaigns"
        columns={columns}
        rows={list.items}
        rowKey={(c) => c.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={state ? `No ${state} campaigns` : "No email campaigns yet"} description="Build one from a template, a segment and a verified sender." />}
      />
    </Card>
  );
}

function useBuilderLookups() {
  const templates = useApi<Page<Template>>("/api/v1/email/templates?limit=100");
  const senders = useApi<Page<Sender>>("/api/v1/email/senders?limit=100");
  const segments = useApi<Page<Segment>>("/api/v1/segments");
  return { templates, senders, segments };
}

type Form = { name: string; template_id: string; segment_id: string; sender_id: string; timezone: string; max_per_minute: number; ends_at: string };

/** Builder for a new campaign, or the edit form of a draft. */
export function CampaignBuilder({ existing, onSaved }: { existing?: Campaign; onSaved?: (c: Campaign) => void }) {
  const router = useRouter();
  const toast = useToast();
  const { templates, senders, segments } = useBuilderLookups();
  const key = useIdempotencyKey(true);
  const [form, setForm] = React.useState<Form>(() => ({
    name: existing?.name ?? "",
    template_id: existing?.template_id ?? "",
    segment_id: existing?.segment_id ?? "",
    sender_id: existing?.sender_id ?? "",
    timezone: existing?.timezone ?? (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"),
    max_per_minute: existing?.max_per_minute ?? 60,
    ends_at: existing?.ends_at ? toLocalInput(existing.ends_at) : "",
  }));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const campaignTemplates = (templates.data?.items ?? []).filter((t) => t.category === "campaign" && t.status !== "archived");
  const allSenders = senders.data?.items ?? [];
  const chosenSender = allSenders.find((s) => s.id === form.sender_id);
  const senderBlock = chosenSender ? senderBlockReason(chosenSender) : null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const endsAt = form.ends_at ? new Date(form.ends_at).toISOString() : null;
    const body = {
      name: form.name.trim(),
      template_id: form.template_id,
      segment_id: form.segment_id,
      sender_id: form.sender_id,
      timezone: form.timezone.trim(),
      max_per_minute: form.max_per_minute,
      ends_at: endsAt,
    };
    try {
      const out = existing
        ? await api<Campaign>(`/api/v1/email/campaigns/${existing.id}`, { method: "PATCH", body: { ...body, expected_version: existing.version } })
        : await api<Campaign>("/api/v1/email/campaigns", { method: "POST", body, idempotencyKey: key || true });
      toast({ tone: "success", title: existing ? "Campaign updated" : "Campaign saved as a draft", description: existing ? undefined : "Review it, then launch or schedule it." });
      if (onSaved) onSaved(out);
      else router.push(`/app/campaigns/email/${out.id}`);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const f = (k: string) => error?.fields[k];
  const formError = error && !error.isConflict && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null;
  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      {error?.isConflict && existing ? <ConflictNotice onReload={() => window.location.reload()} /> : null}
      <Card className="flex flex-col gap-4 p-5">
        <Field id="ec-name" label="Name" required error={f("name")}>
          {(p) => <Input {...p} value={form.name} maxLength={200} onChange={(e) => set("name", e.target.value)} placeholder="Spring check-up reminder" />}
        </Field>
        <div className="grid gap-4 md:grid-cols-3">
          <Field id="ec-template" label="Template" required error={f("template_id")} hint={<Link className="text-brand hover:underline" href="/app/campaigns/email/templates">Manage templates</Link>}>
            {(p) => (
              <Select {...p} value={form.template_id} onChange={(e) => set("template_id", e.target.value)} disabled={templates.loading}>
                <option value="">{templates.loading ? "Loading…" : "Select a template"}</option>
                {campaignTemplates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} (v{t.current_version})
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="ec-segment" label="Audience segment" required error={f("segment_id")} hint="Contacts without email consent are skipped automatically.">
            {(p) =>
              segments.error ? (
                <p className="text-xs text-danger">Segments could not be loaded ({segments.error.message})</p>
              ) : (
                <Select {...p} value={form.segment_id} onChange={(e) => set("segment_id", e.target.value)} disabled={segments.loading}>
                  <option value="">{segments.loading ? "Loading…" : "Select a segment"}</option>
                  {(segments.data?.items ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </Select>
              )
            }
          </Field>
          <Field id="ec-sender" label="Sender" required error={f("sender_id") ?? (senderBlock ? `${senderBlock}: this campaign cannot launch until it is fixed.` : undefined)} hint={<Link className="text-brand hover:underline" href="/admin/email">Senders &amp; domains</Link>}>
            {(p) => (
              <Select {...p} value={form.sender_id} onChange={(e) => set("sender_id", e.target.value)} disabled={senders.loading}>
                <option value="">{senders.loading ? "Loading…" : "Select a sender"}</option>
                {allSenders.map((s) => {
                  const block = senderBlockReason(s);
                  return (
                    <option key={s.id} value={s.id} disabled={block !== null && s.id !== form.sender_id}>
                      {s.from_name} &lt;{s.from_address}&gt;{block ? ` — ${block}` : ""}
                    </option>
                  );
                })}
              </Select>
            )}
          </Field>
        </div>
        {!templates.loading && campaignTemplates.length === 0 ? <Notice tone="warn">There are no campaign templates yet. Create one first; transactional templates cannot be used for campaigns.</Notice> : null}
        {!senders.loading && allSenders.every((s) => senderBlockReason(s) !== null) ? (
          <Notice tone="warn" title="No sender can send yet">
            Verify a sending domain and add a sender identity on it under <Link className="text-brand hover:underline" href="/admin/email">Email senders</Link>.
          </Notice>
        ) : null}
      </Card>
      <Card className="flex flex-col gap-4 p-5">
        <h2 className="text-sm font-semibold">Sending window and pace</h2>
        <div className="grid gap-4 md:grid-cols-3">
          <Field id="ec-tz" label="Time zone" hint="Used for the workspace email window." error={f("timezone")}>
            {(p) => <Input {...p} value={form.timezone} onChange={(e) => set("timezone", e.target.value)} spellCheck={false} />}
          </Field>
          <Field id="ec-pace" label="Maximum per minute" hint="1 to 10,000. Lower is gentler on your domain reputation." error={f("max_per_minute")}>
            {(p) => <Input {...p} type="number" min={1} max={10000} value={form.max_per_minute} onChange={(e) => set("max_per_minute", Number(e.target.value))} />}
          </Field>
          <Field id="ec-ends" label="Stop sending after (optional)" hint="Anything not sent by then is not sent." error={f("ends_at")}>
            {(p) => <Input {...p} type="datetime-local" value={form.ends_at} onChange={(e) => set("ends_at", e.target.value)} />}
          </Field>
        </div>
        <p className="text-xs text-muted">You choose when to start after saving: launch now or schedule a start time.</p>
      </Card>
      <FormError message={formError} />
      <div className="flex justify-end gap-2">
        {!existing ? (
          <Link href="/app/campaigns/email" className="inline-flex h-9 items-center rounded-md border border-border px-4 text-sm hover:bg-surface-2">
            Cancel
          </Link>
        ) : null}
        <Button type="submit" disabled={busy || !form.name.trim() || !form.template_id || !form.segment_id || !form.sender_id} data-testid="save-campaign">
          {busy ? "Saving…" : existing ? "Save changes" : "Save draft"}
        </Button>
      </div>
    </form>
  );
}

export function CampaignDetail({ campaignId }: { campaignId: string }) {
  const can = useCan();
  const toast = useToast();
  const res = useApi<Campaign>(`/api/v1/email/campaigns/${campaignId}`);
  const report = useApi<Report>(`/api/v1/email/campaigns/${campaignId}/report`);
  const lookups = useBuilderLookups();
  const [editing, setEditing] = React.useState(false);
  const [scheduling, setScheduling] = React.useState(false);
  const [action, setAction] = React.useState<"pause" | "resume" | "cancel" | null>(null);
  const live = res.data ? isCampaignLive(res.data) : false;

  React.useEffect(() => {
    if (!live) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") {
        report.reload();
        res.reload();
      }
    }, 15_000);
    return () => clearInterval(t);
  }, [live]); // eslint-disable-line react-hooks/exhaustive-deps

  if (res.loading && !res.data) return <Card><LoadingState label="Loading campaign" /></Card>;
  if (res.error && !res.data) return <Card><ErrorState problem={res.error.problem} onRetry={res.reload} /></Card>;
  const c = res.data;
  if (!c) return null;
  const allowed = campaignActions(c.state);
  const manage = can("emails:campaigns:manage");
  const template = lookups.templates.data?.items.find((t) => t.id === c.template_id);
  const sender = lookups.senders.data?.items.find((s) => s.id === c.sender_id);
  const segment = lookups.segments.data?.items.find((s) => s.id === c.segment_id);
  const senderBlock = sender ? senderBlockReason(sender) : null;
  const refresh = () => {
    res.reload();
    report.reload();
  };

  return (
    <>
      <PageHeader
        title={c.name}
        description={`Created ${formatDateTime(c.created_at)} · version ${c.version}`}
        actions={
          manage ? (
            <div className="flex flex-wrap gap-2">
              {allowed.edit ? (
                <Button variant="secondary" onClick={() => setEditing((v) => !v)}>
                  {editing ? "Close editor" : "Edit"}
                </Button>
              ) : null}
              {allowed.pause ? (
                <Button variant="secondary" onClick={() => setAction("pause")}>
                  Pause
                </Button>
              ) : null}
              {allowed.resume ? (
                <Button variant="secondary" onClick={() => setAction("resume")}>
                  Resume
                </Button>
              ) : null}
              {allowed.cancel ? (
                <Button variant="ghost" onClick={() => setAction("cancel")}>
                  Cancel campaign
                </Button>
              ) : null}
              {allowed.schedule ? (
                <Button onClick={() => setScheduling(true)} disabled={senderBlock !== null} title={senderBlock ?? undefined} data-testid="open-launch">
                  Launch or schedule
                </Button>
              ) : null}
            </div>
          ) : null
        }
      />
      {senderBlock && allowed.schedule ? (
        <div className="mb-4">
          <Notice tone="warn" title="This campaign cannot launch yet">
            {senderBlock}. Verify the sender&apos;s domain under <Link className="text-brand hover:underline" href="/admin/email">Email senders</Link>.
          </Notice>
        </div>
      ) : null}
      {c.state === "paused" && c.pause_reason ? (
        <div className="mb-4">
          <Notice tone="warn" title="Paused">
            {c.pause_reason.replace(/_/g, " ")}
          </Notice>
        </div>
      ) : null}
      {c.last_error ? (
        <div className="mb-4">
          <Notice tone="danger" title="Last error">
            {c.last_error}
          </Notice>
        </div>
      ) : null}
      {editing && allowed.edit ? (
        <div className="mb-4">
          <CampaignBuilder
            existing={c}
            onSaved={() => {
              setEditing(false);
              res.reload();
            }}
          />
        </div>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <ReportCard report={report} />
          <MessagesCard campaignId={campaignId} />
        </div>
        <Card>
          <CardHeader title="Setup" />
          <div className="px-5 py-4">
            <KeyValues
              items={[
                { label: "State", value: <Badge tone={campaignTone(c.state)}>{c.state}</Badge> },
                { label: "Template", value: template ? <Link className="text-brand hover:underline" href={`/app/campaigns/email/templates/${template.id}`}>{template.name}</Link> : "—" },
                { label: "Template version", value: c.template_version ? `v${c.template_version} (pinned at launch)` : "Pinned when launched" },
                { label: "Segment", value: segment?.name ?? "—" },
                { label: "Sender", value: sender ? <span>{sender.from_name} <span className="font-mono text-xs text-muted">&lt;{sender.from_address}&gt;</span></span> : "—" },
                { label: "Time zone", value: c.timezone },
                { label: "Pace", value: `${c.max_per_minute} per minute` },
                { label: "Starts", value: c.scheduled_at ? formatDateTime(c.scheduled_at) : "Not scheduled" },
                { label: "Stops after", value: c.ends_at ? formatDateTime(c.ends_at) : "—" },
                { label: "Started", value: c.started_at ? formatDateTime(c.started_at) : "—" },
                { label: "Completed", value: c.completed_at ? formatDateTime(c.completed_at) : "—" },
              ]}
            />
          </div>
        </Card>
      </div>
      <ScheduleDialog open={scheduling} campaign={c} onClose={() => setScheduling(false)} onDone={() => { setScheduling(false); refresh(); }} />
      <ConfirmDialog
        open={action !== null}
        onClose={() => setAction(null)}
        title={action === "pause" ? "Pause this campaign?" : action === "resume" ? "Resume this campaign?" : "Cancel this campaign?"}
        description={
          action === "cancel"
            ? "Messages not yet sent are dropped. This cannot be undone."
            : action === "pause"
              ? "Sending stops after the messages already handed to the provider. You can resume later."
              : "Sending continues inside the email window."
        }
        confirmLabel={action === "pause" ? "Pause" : action === "resume" ? "Resume" : "Cancel campaign"}
        tone={action === "cancel" ? "danger" : "primary"}
        reason={action === "cancel" ? { label: "Reason (recorded in the audit log)", minLength: 3, maxLength: 200 } : undefined}
        onConfirm={async (reason) => {
          if (!action) return;
          try {
            await api(`/api/v1/email/campaigns/${campaignId}/${action}`, { method: "POST", body: { reason: reason || null } });
            toast({ tone: "success", title: action === "pause" ? "Campaign paused" : action === "resume" ? "Campaign resumed" : "Campaign canceled" });
          } finally {
            refresh();
          }
        }}
      />
    </>
  );
}

function ScheduleDialog({ open, campaign, onClose, onDone }: { open: boolean; campaign: Campaign; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const key = useIdempotencyKey(open);
  const [mode, setMode] = React.useState<"now" | "later">("now");
  const [at, setAt] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setMode("now");
      setAt("");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "now") {
        await api(`/api/v1/email/campaigns/${campaign.id}/launch`, { method: "POST", idempotencyKey: key || true });
      } else {
        const body = scheduleBody(at);
        if (!body || !("scheduled_at" in body)) throw new Error("Choose a valid start time.");
        await api(`/api/v1/email/campaigns/${campaign.id}/schedule`, { method: "POST", body, idempotencyKey: key || true });
      }
      toast({ tone: "success", title: mode === "now" ? "Campaign launched" : "Campaign scheduled", description: "Sending happens inside the workspace email window." });
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  const code = typeof error?.problem.code_detail === "string" ? error.problem.code_detail : null;
  const message =
    code === "sender_domain_not_verified"
      ? "The sender's domain is not verified. Verify it under Email senders, then try again."
      : code === "emails_disabled"
        ? "Email sending is turned off for this workspace (kill switch or policy)."
        : error && !error.isStepUp
          ? error.message
          : null;
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title={`Launch “${campaign.name}”`} description="The audience is resolved when sending starts. Consent and suppression are checked for every message.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-2 text-sm" role="radiogroup" aria-label="When to start">
          <label className="inline-flex items-center gap-2">
            <input type="radio" name="ec-when" checked={mode === "now"} onChange={() => setMode("now")} /> Start now
          </label>
          <label className="inline-flex items-center gap-2">
            <input type="radio" name="ec-when" checked={mode === "later"} onChange={() => setMode("later")} /> Schedule a start time
          </label>
        </div>
        {mode === "later" ? (
          <Field id="ec-at" label="Start at (your time zone)" required error={error?.fields.scheduled_at}>
            {(p) => <Input {...p} type="datetime-local" value={at} min={toLocalInput(new Date())} onChange={(e) => setAt(e.target.value)} />}
          </Field>
        ) : null}
        <FormError message={message} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || (mode === "later" && !at)} data-testid="confirm-launch">
            {busy ? "Working…" : mode === "now" ? "Launch now" : "Schedule"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

const SEGMENT_CLASS: Record<string, string> = { delivered: "bg-ok", accepted: "bg-brand", problem: "bg-danger" };

function ReportCard({ report }: { report: ApiState<Report> }) {
  if (report.loading && !report.data) return <Card><LoadingState label="Loading report" /></Card>;
  if (report.error && !report.data) return <Card><ErrorState problem={report.error.problem} onRetry={report.reload} /></Card>;
  const r = report.data;
  if (!r) return null;
  const rows = reportRows(r);
  const bar = reportSegments(rows);
  const rates = reportRates(r.rates);
  const swatch = (state: string, group: string) => (state === "delivered" ? "bg-ok" : state === "accepted" ? "bg-brand" : group === "problem" ? "bg-danger" : "bg-surface-2 ring-1 ring-border");
  return (
    <Card>
      <CardHeader title="Delivery report" description="What the email provider reported. Opens and clicks are not tracked: they are unreliable and invade recipients' privacy." />
      <div className="flex flex-col gap-4 px-5 py-4">
        <div className="grid gap-3 sm:grid-cols-4">
          <Stat label="Recipients" value={r.recipients.toLocaleString()} hint={r.audience_complete ? "Audience fully resolved" : "Audience still being resolved"} />
          <Stat label="Handed to provider" value={r.sent.toLocaleString()} />
          {rates.slice(0, 2).map((x) => (
            <Stat key={x.key} label={x.label} value={percent(x.value)} hint="of messages handed to the provider" />
          ))}
        </div>
        {bar.total > 0 ? (
          <div
            role="img"
            aria-label={`Delivery outcomes: ${bar.segments.map((s) => `${s.label} ${s.count} (${percent(s.share)})`).join(", ")}; still in flight ${bar.inFlight}.`}
            className="flex h-2 w-full gap-0.5 overflow-hidden rounded-full bg-surface-2"
          >
            {bar.segments
              .filter((s) => s.count > 0)
              .map((s) => (
                <span key={s.key} className={cn("h-full first:rounded-l-full last:rounded-r-full", SEGMENT_CLASS[s.key])} style={{ width: `${s.share * 100}%` }} title={`${s.label}: ${s.count.toLocaleString()} (${percent(s.share)})`} />
              ))}
          </div>
        ) : null}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Messages by delivery state</caption>
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th className="py-1.5 pr-3">State</th>
                <th className="py-1.5 pr-3">What it means</th>
                <th className="py-1.5 pr-3 text-right">Messages</th>
                <th className="py-1.5 text-right">Share</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.state} className={cn("border-t border-border", row.count === 0 && "text-muted")}>
                  <td className="py-1.5 pr-3">
                    <span className="inline-flex items-center gap-2">
                      <span aria-hidden className={cn("inline-block h-2.5 w-2.5 rounded-full", swatch(row.state, row.group))} />
                      {row.label}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-xs text-muted">{row.meaning}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{row.count.toLocaleString()}</td>
                  <td className="tabular py-1.5 text-right">{percent(row.share)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rates.length > 2 ? (
          <p className="text-xs text-muted">
            {rates
              .slice(2)
              .map((x) => `${x.label}: ${percent(x.value, 2)}`)
              .join(" · ")}{" "}
            (of messages handed to the provider)
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function MessagesCard({ campaignId }: { campaignId: string }) {
  const [state, setState] = React.useState("");
  const list = useCursorList<OutboundMessage>(withQuery("/api/v1/email/messages", { campaign_id: campaignId, state }));
  const columns: Column<OutboundMessage>[] = [
    { key: "to", header: "To", cell: (m) => <span className="font-mono text-xs">{m.to_masked ?? "—"}</span> },
    { key: "state", header: "State", cell: (m) => <Badge tone={m.state === "delivered" ? "ok" : ["bounced", "complained", "rejected", "failed"].includes(m.state) ? "danger" : "neutral"}>{m.state}</Badge> },
    { key: "changed", header: "Updated", cell: (m) => <span title={formatDateTime(m.state_changed_at)}>{relativeTime(m.state_changed_at)}</span> },
    { key: "error", header: "Detail", cell: (m) => <span className="text-xs text-muted">{m.last_error ?? (m.attempts > 1 ? `${m.attempts} attempts` : "")}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader
        title="Messages"
        description="Recipient addresses are masked."
        actions={
          <div>
            <label htmlFor="ec-msg-state" className="sr-only">
              Message state
            </label>
            <Select id="ec-msg-state" value={state} onChange={(e) => setState(e.target.value)} className="w-40">
              <option value="">All states</option>
              {["pending", "queued", "sending", "accepted", "delivered", "bounced", "complained", "rejected", "unsubscribed", "failed"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </div>
        }
      />
      <DataTable
        caption="Campaign messages"
        columns={columns}
        rows={list.items}
        rowKey={(m) => m.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No messages yet" description="Messages are created when the campaign starts sending." />}
      />
    </Card>
  );
}

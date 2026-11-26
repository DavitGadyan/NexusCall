"use client";
/**
 * Campaign detail: lifecycle actions (schedule, launch with confirmation, pause, resume, cancel), the report
 * (counts by state and by outcome), the per-contact list and the configuration. Running campaigns refresh
 * on `call.updated` live hints and by polling.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { useLiveEvents } from "@/lib/client/use-live-events";
import { formatDateTime, humanize, relativeTime, shortId } from "@/lib/format";
import {
  Button,
  ButtonLink,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  ConflictNotice,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValues,
  LoadingState,
  Notice,
  PageHeader,
  Stat,
  Tabs,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { FilterSelect } from "@/components/crm/Contacts";
import { isVersionConflict, newIdempotencyKey } from "@/components/crm/lib/http";
import { usePoll } from "@/components/crm/lib/use-poll";
import { BarList } from "./BarList";
import { StateBadge } from "./Campaigns";
import {
  ACTION_PERMISSION,
  actionsFor,
  type Campaign,
  type CampaignContact,
  type CampaignReport,
  editableFields,
  problemLabel,
  problemsOf,
} from "./lib/campaign-form";

const ACTION_LABEL: Record<string, string> = { schedule: "Schedule", unschedule: "Back to draft", launch: "Launch", pause: "Pause", resume: "Resume", cancel: "Cancel campaign" };
const CONTACT_STATES = ["pending", "in_flight", "retry_wait", "completed", "failed", "skipped"];

export function CampaignDetail({ campaignId }: { campaignId: string }) {
  const can = useCan();
  const toast = useToast();
  const base = `/api/v1/campaigns/${campaignId}`;
  const campaign = useApi<Campaign>(base);
  const report = useApi<CampaignReport>(`${base}/report`);
  const [tab, setTab] = React.useState("report");
  const [confirm, setConfirm] = React.useState<"launch" | "cancel" | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<ApiError | null>(null);
  const launchKey = React.useRef<string>("");
  const c = campaign.data;
  const live = Boolean(c && (c.state === "running" || c.state === "paused" || c.snapshot_status === "pending" || c.snapshot_status === "running"));

  const refresh = React.useCallback(() => {
    campaign.reload();
    report.reload();
  }, [campaign, report]);
  usePoll(refresh, live);
  const debounce = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  useLiveEvents(
    () => {
      if (debounce.current) return;
      debounce.current = setTimeout(() => {
        debounce.current = null;
        report.reload();
      }, 2000);
    },
    { events: ["call.updated"], enabled: live && can("calls:read") },
  );

  if (campaign.loading && !c) return <Card><LoadingState /></Card>;
  if (campaign.error && !c) return <Card><ErrorState problem={campaign.error.problem} onRetry={campaign.reload} /></Card>;
  if (!c) return null;

  async function run(action: string): Promise<void> {
    if (!c) return;
    setBusy(action);
    setActionError(null);
    try {
      const out = await api<Campaign>(`${base}/${action}`, {
        method: "POST",
        body: { expected_version: c.version },
        idempotencyKey: action === "launch" ? launchKey.current : undefined,
      });
      campaign.setData(() => out);
      report.reload();
      toast({ tone: "success", title: action === "launch" ? "Campaign launched" : `Campaign ${out.state}`, description: action === "launch" ? "The audience snapshot is being built; calls start inside the window." : undefined });
    } catch (err) {
      const e = toApiError(err);
      setActionError(e);
      throw e;
    } finally {
      setBusy(null);
    }
  }

  const actions = actionsFor(c).filter((a) => can(ACTION_PERMISSION[a] ?? "campaigns:manage"));
  const editable = editableFields(c.state) !== "none" && can(c.state === "draft" ? "campaigns:create" : "campaigns:manage");
  const problems = actionError ? problemsOf(actionError.problem) : [];

  return (
    <>
      <PageHeader
        title={c.name}
        description={c.description ?? undefined}
        actions={
          <div className="flex flex-wrap gap-2">
            {editable ? <ButtonLink href={`/app/campaigns/calls/${c.id}/edit`}>Edit</ButtonLink> : null}
            {actions.map((a) => (
              <Button
                key={a}
                variant={a === "launch" ? "primary" : a === "cancel" ? "danger" : "secondary"}
                disabled={busy !== null}
                data-testid={`campaign-${a}`}
                onClick={() => {
                  if (a === "launch" || a === "cancel") {
                    if (a === "launch") launchKey.current = newIdempotencyKey();
                    setConfirm(a);
                  } else run(a).catch(() => undefined);
                }}
              >
                {busy === a ? "Working…" : ACTION_LABEL[a] ?? humanize(a)}
              </Button>
            ))}
          </div>
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
        <StateBadge state={c.state} />
        {c.launched_at ? <span className="text-muted">Launched {relativeTime(c.launched_at)}</span> : null}
        {c.completed_at ? <span className="text-muted">Finished {relativeTime(c.completed_at)}</span> : null}
        {c.failure_reason ? <span className="text-danger">{humanize(c.failure_reason)}</span> : null}
      </div>
      {actionError && !actionError.isStepUp && !confirm ? (
        <div className="mb-4">
          {isVersionConflict(actionError) ? (
            <ConflictNotice onReload={() => { setActionError(null); refresh(); }}>{actionError.message}</ConflictNotice>
          ) : (
            <Notice tone="danger" title={actionError.message}>
              {problems.length ? <ul className="list-disc pl-4">{problems.map((p) => <li key={p}>{problemLabel(p)}</li>)}</ul> : null}
            </Notice>
          )}
        </div>
      ) : null}
      <Tabs label="Campaign sections" value={tab} onChange={setTab} tabs={[{ id: "report", label: "Report" }, { id: "contacts", label: "Contacts" }, { id: "settings", label: "Configuration" }]}>
        {tab === "report" ? <ReportView report={report.data} loading={report.loading} error={report.error} onRetry={report.reload} /> : null}
        {tab === "contacts" ? <ContactsView campaignId={c.id} live={live} /> : null}
        {tab === "settings" ? <SettingsView c={c} /> : null}
      </Tabs>
      <ConfirmDialog
        open={confirm === "launch"}
        onClose={() => setConfirm(null)}
        title="Launch campaign"
        tone="primary"
        confirmLabel="Launch"
        description={
          <span className="flex flex-col gap-1">
            <span>The AI agent starts calling the segment&apos;s contactable contacts, {c.window_start}–{c.window_end} ({c.timezone}) on {c.window_days.join(", ")}.</span>
            <span>Pacing: {c.calls_per_minute} calls/min, at most {c.max_concurrency} at once, {c.max_attempts} attempt{c.max_attempts === 1 ? "" : "s"} per contact.</span>
            <span>Suppressed contacts and contacts without consent are skipped. The workspace kill switch stops everything at once.</span>
          </span>
        }
        typedConfirmation={c.name.length <= 60 ? c.name : "LAUNCH"}
        onConfirm={async () => {
          await run("launch");
        }}
      >
        {confirm === "launch" && actionError && problems.length ? (
          <Notice tone="danger" title="Cannot launch yet"><ul className="list-disc pl-4">{problems.map((p) => <li key={p}>{problemLabel(p)}</li>)}</ul></Notice>
        ) : null}
      </ConfirmDialog>
      <ConfirmDialog
        open={confirm === "cancel"}
        onClose={() => setConfirm(null)}
        title="Cancel campaign"
        description="Everything not yet called is skipped and queued calls are cancelled. This cannot be undone; the history stays."
        confirmLabel="Cancel campaign"
        typedConfirmation="CANCEL"
        onConfirm={async () => {
          await run("cancel");
        }}
      />
    </>
  );
}

function ReportView({ report, loading, error, onRetry }: { report?: CampaignReport; loading: boolean; error?: ApiError; onRetry: () => void }) {
  if (loading && !report) return <Card><LoadingState /></Card>;
  if (error && !report) return <Card><ErrorState problem={error.problem} onRetry={onRetry} /></Card>;
  if (!report) return null;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Audience" value={report.audience.toLocaleString()} hint={report.snapshot_status === "completed" ? "Snapshot complete" : `Snapshot ${report.snapshot_status}`} />
        <Stat label="Calls dispatched" value={report.calls_dispatched.toLocaleString()} />
        <Stat label="Attempts" value={report.attempts.toLocaleString()} />
        <Stat label="Completed" value={(report.contacts_by_state.completed ?? 0).toLocaleString()} hint={report.audience ? `${Math.round(((report.contacts_by_state.completed ?? 0) / report.audience) * 100)}% of the audience` : undefined} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Contacts by state" />
          <div className="py-2"><BarList counts={report.contacts_by_state} caption="Contacts by state" empty="No contacts yet. The audience is snapshotted at launch." /></div>
        </Card>
        <Card>
          <CardHeader title="Outcomes" />
          <div className="py-2"><BarList counts={report.outcomes} caption="Call outcomes" empty="No outcomes yet." /></div>
        </Card>
      </div>
    </div>
  );
}

function ContactsView({ campaignId, live }: { campaignId: string; live: boolean }) {
  const [state, setState] = React.useState("");
  const list = useCursorList<CampaignContact>(withQuery(`/api/v1/campaigns/${campaignId}/contacts`, { state }));
  usePoll(list.reload, live);
  const columns: Column<CampaignContact>[] = [
    { key: "contact", header: "Contact", cell: (r) => <Link href={`/app/crm/contacts/${r.contact_id}`} className="font-mono text-xs text-brand hover:underline">{shortId(r.contact_id)}</Link> },
    { key: "state", header: "State", cell: (r) => <span>{humanize(r.state)}{r.skip_reason ? <span className="block text-xs text-muted">{humanize(r.skip_reason)}</span> : null}</span> },
    { key: "attempts", header: "Attempts", cell: (r) => <span className="tabular">{r.attempts}</span> },
    { key: "outcome", header: "Outcome", cell: (r) => (r.outcome ? humanize(r.outcome) : "—") },
    { key: "next", header: "Next attempt", cell: (r) => (r.next_attempt_at ? <span title={formatDateTime(r.next_attempt_at)}>{relativeTime(r.next_attempt_at)}</span> : "—"), hideOnMobile: true },
    { key: "call", header: "Last call", cell: (r) => (r.last_call_id ? <Link href={`/app/calls/${r.last_call_id}`} className="font-mono text-xs text-brand hover:underline">{shortId(r.last_call_id)}</Link> : "—"), hideOnMobile: true },
  ];
  return (
    <Card>
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
        <FilterSelect id="cc-state" label="State" value={state} onChange={setState} options={CONTACT_STATES} />
      </div>
      <DataTable caption="Campaign contacts" columns={columns} rows={list.items} rowKey={(r) => r.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No contacts" description="The audience is snapshotted when the campaign launches." />} />
    </Card>
  );
}

function SettingsView({ c }: { c: Campaign }) {
  return (
    <Card>
      <div className="px-5 py-4">
        <KeyValues
          items={[
            { label: "Agent", value: c.agent_id ? <Link className="text-brand hover:underline" href={`/app/agents/${c.agent_id}`}>{shortId(c.agent_id)}</Link> : "Not set" },
            { label: "Segment", value: c.segment_id ? <Link className="text-brand hover:underline" href={`/app/crm/segments/${c.segment_id}`}>{shortId(c.segment_id)}</Link> : "Not set" },
            { label: "Timezone", value: c.timezone },
            { label: "Window", value: `${c.window_start}–${c.window_end} on ${c.window_days.join(", ")}` },
            { label: "Pacing", value: `${c.calls_per_minute} calls/min, ${c.max_concurrency} concurrent` },
            { label: "Attempts", value: `${c.max_attempts}, retry after ${Math.round(c.retry_delay_seconds / 60)} min` },
            { label: "Start", value: formatDateTime(c.start_at) },
            { label: "End", value: formatDateTime(c.end_at) },
            { label: "Created", value: formatDateTime(c.created_at) },
            { label: "Version", value: c.version },
          ]}
        />
      </div>
    </Card>
  );
}

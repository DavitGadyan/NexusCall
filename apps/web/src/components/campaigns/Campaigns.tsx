"use client";
/** Call campaigns list with state badges, and the tenant call kill switch (typed confirmation + step-up). */
import { OctagonX } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, ButtonLink, Card, CardHeader, type Column, ConfirmDialog, DataTable, EmptyState, ErrorState, LoadingState, Notice, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { StepUpHint } from "@/components/crm/common";
import { FilterSelect } from "@/components/crm/Contacts";
import { type Campaign, type KillSwitch, STATE_TONE } from "./lib/campaign-form";

const STATES = ["draft", "scheduled", "running", "paused", "completed", "canceled", "failed"];

export function StateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONE[state] ?? "neutral"}>{state}</Badge>;
}

export function Campaigns() {
  const can = useCan();
  const [state, setState] = React.useState("");
  const list = useCursorList<Campaign>(withQuery("/api/v1/campaigns", { state }));
  const columns: Column<Campaign>[] = [
    { key: "name", header: "Campaign", cell: (c) => <Link href={`/app/campaigns/calls/${c.id}`} className="font-medium text-brand hover:underline">{c.name}</Link> },
    { key: "state", header: "State", cell: (c) => <StateBadge state={c.state} /> },
    { key: "audience", header: "Audience", cell: (c) => <span className="tabular">{c.snapshot_status === "completed" ? c.snapshot_total.toLocaleString() : c.snapshot_status === "none" ? "—" : c.snapshot_status}</span>, hideOnMobile: true },
    { key: "window", header: "Window", cell: (c) => <span className="whitespace-nowrap">{c.window_start}–{c.window_end} <span className="text-xs text-muted">{c.timezone}</span></span>, hideOnMobile: true },
    { key: "pacing", header: "Pacing", cell: (c) => <span className="tabular whitespace-nowrap">{c.calls_per_minute}/min · {c.max_concurrency} max</span>, hideOnMobile: true },
    { key: "updated", header: "Updated", cell: (c) => <span title={formatDateTime(c.updated_at)}>{relativeTime(c.updated_at)}</span> },
  ];
  return (
    <div className="flex flex-col gap-4">
      <KillSwitchCard />
      <Card>
        <CardHeader
          title="Call campaigns"
          description="Outbound calling by an AI voice agent to a segment, inside a calling window."
          actions={can("campaigns:create") ? <ButtonLink size="sm" variant="primary" href="/app/campaigns/calls/new" data-testid="new-campaign">New campaign</ButtonLink> : null}
        />
        <div className="flex flex-wrap gap-2 border-b border-border px-4 py-3">
          <FilterSelect id="campaign-state" label="State" value={state} onChange={setState} options={STATES} />
        </div>
        <DataTable
          caption="Call campaigns"
          columns={columns}
          rows={list.items}
          rowKey={(c) => c.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title={state ? `No ${state} campaigns` : "No campaigns yet"} description={state ? undefined : "Create a campaign from an AI voice agent and a segment."} />}
        />
      </Card>
    </div>
  );
}

export const ENGAGE_PHRASE = "STOP ALL CALLS";
export const RELEASE_PHRASE = "RESUME CALLS";

export function KillSwitchCard() {
  const can = useCan();
  const toast = useToast();
  const ks = useApi<KillSwitch>("/api/v1/campaigns/kill-switch");
  const [open, setOpen] = React.useState(false);
  const engaged = ks.data?.engaged ?? false;
  return (
    <Card className={engaged ? "border-danger/50" : undefined}>
      <CardHeader
        title="Workspace call kill switch"
        description="Stops every outbound campaign call in this workspace immediately. Campaigns keep their state but dispatch nothing while it is engaged."
        actions={
          can("campaigns:manage") && ks.data ? (
            <Button size="sm" variant={engaged ? "secondary" : "danger"} onClick={() => setOpen(true)} data-testid="kill-switch">
              <OctagonX className="h-4 w-4" aria-hidden /> {engaged ? "Release" : "Stop all calls"}
            </Button>
          ) : null
        }
      />
      <div className="px-5 py-3">
        {ks.loading && !ks.data ? <LoadingState rows={1} /> : ks.error && !ks.data ? <ErrorState problem={ks.error.problem} onRetry={ks.reload} /> : ks.data ? (
          engaged ? (
            <Notice tone="danger" title="Calls are stopped">
              {ks.data.reason ? `Reason: ${ks.data.reason}. ` : ""}Engaged {ks.data.updated_at ? relativeTime(ks.data.updated_at) : ""}{ks.data.changed_by ? ` by ${ks.data.changed_by}` : ""}.
            </Notice>
          ) : (
            <p className="text-sm text-muted"><Badge tone="ok">Calls allowed</Badge>{ks.data.updated_at ? <span className="ml-2">Last changed {relativeTime(ks.data.updated_at)}.</span> : null}</p>
          )
        ) : null}
      </div>
      <ConfirmDialog
        open={open}
        onClose={() => setOpen(false)}
        title={engaged ? "Release the call kill switch" : "Stop all calls"}
        tone={engaged ? "primary" : "danger"}
        description={
          <span className="flex flex-col gap-2">
            <span>{engaged ? "Running campaigns may dispatch calls again. Paused campaigns stay paused until someone resumes them." : "No new campaign calls will be dispatched in this workspace until the switch is released. Calls in progress are not cut off."}</span>
            <span><StepUpHint /> Changing the switch needs a recent MFA sign-in and is audited.</span>
          </span>
        }
        confirmLabel={engaged ? "Release" : "Stop all calls"}
        typedConfirmation={engaged ? RELEASE_PHRASE : ENGAGE_PHRASE}
        reason={{ label: "Reason (recorded in the audit log)", minLength: 5, maxLength: 300 }}
        onConfirm={async (reason) => {
          const out = await api<KillSwitch>("/api/v1/campaigns/kill-switch", { method: "POST", body: { engaged: !engaged, reason } });
          ks.setData(() => out);
          toast(out.engaged ? { tone: "error", title: "All campaign calls stopped" } : { tone: "success", title: "Kill switch released" });
        }}
      />
    </Card>
  );
}

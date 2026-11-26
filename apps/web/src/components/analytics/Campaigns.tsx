"use client";
/** Campaign drill-down: pick a campaign, then its call funnel, costs, email delivery and daily trend. */
import Link from "next/link";
import * as React from "react";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { Badge, Card, CardHeader, type Column, DataTable, EmptyState, ErrorState, LoadingState } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { FunnelBars, KpiTile } from "./charts";
import { TimeseriesCard } from "./Overview";
import { RangeFilter } from "./RangeFilter";
import { type DateRange, presetRange } from "./lib/dates";
import { formatCompact, formatDuration, formatInt, formatPercent, formatUsd } from "./lib/format";
import { callFunnel, emailFunnel } from "./lib/funnel";
import type { CampaignReport as Report } from "./lib/types";

type CampaignRow = { id: string; name: string; state: string; launched_at: string | null; created_at: string };

export function CampaignPicker() {
  const can = useCan();
  const list = useCursorList<CampaignRow>(can("campaigns:read") ? "/api/v1/campaigns" : null);
  if (!can("campaigns:read")) {
    return (
      <Card>
        <EmptyState title="Campaign list unavailable" description="Listing campaigns needs the campaigns:read permission. Open a campaign report from a link instead." />
      </Card>
    );
  }
  const columns: Column<CampaignRow>[] = [
    {
      key: "name",
      header: "Campaign",
      cell: (c) => (
        <Link href={`/app/analytics/campaigns/${c.id}`} className="font-medium text-brand hover:underline">
          {c.name}
        </Link>
      ),
    },
    { key: "state", header: "State", cell: (c) => <Badge>{c.state.replace(/_/g, " ")}</Badge> },
    { key: "launched", header: "Launched", hideOnMobile: true, cell: (c) => formatDateTime(c.launched_at) },
  ];
  return (
    <Card>
      <CardHeader title="Call campaigns" description="Choose a campaign to see its results." />
      <DataTable
        caption="Campaigns"
        columns={columns}
        rows={list.items}
        rowKey={(c) => c.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No campaigns yet" />}
      />
    </Card>
  );
}

export function CampaignReport({ campaignId }: { campaignId: string }) {
  const report = useApi<Report>(`/api/v1/analytics/campaigns/${campaignId}`);
  const [range, setRange] = React.useState<DateRange>(() => presetRange(30));
  const r = report.data;

  return (
    <div className="flex flex-col gap-4">
      <Link href="/app/analytics/campaigns" className="text-sm text-brand hover:underline">
        ← All campaigns
      </Link>
      {report.loading && !r ? (
        <Card>
          <LoadingState label="Loading campaign report" />
        </Card>
      ) : report.error && !r ? (
        <Card>
          <ErrorState problem={report.error.problem} onRetry={report.reload} />
        </Card>
      ) : r ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <KpiTile label="Calls attempted" value={formatCompact(r.calls.attempted)} hint={r.launched_at ? `Launched ${formatDateTime(r.launched_at)}` : "Not launched"} />
            <KpiTile label="Answer rate" value={formatPercent(r.calls.answer_rate)} hint={`Average call ${formatDuration(r.calls.average_duration_seconds)}`} />
            <KpiTile label="Qualified" value={formatCompact(r.calls.qualified)} hint={`${formatInt(r.calls.opted_out)} opted out`} />
            <KpiTile label="Call cost" value={formatUsd(r.calls.cost_micros)} hint={`${formatUsd(r.calls.cost_per_call_micros, { precise: true })} per call`} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader title="Call funnel" description={`${formatInt(r.calls.failed)} failed · ${formatInt(r.calls.ended)} ended`} />
              <div className="py-3">
                <FunnelBars steps={callFunnel({ ...r.calls, canceled: 0, total_duration_seconds: 0 })} caption="Campaign call funnel" />
              </div>
            </Card>
            <Card>
              <CardHeader title="Email delivery" description={`${formatInt(r.emails.messages)} messages · delivery rate ${formatPercent(r.emails.delivery_rate)}`} />
              <div className="py-3">
                <FunnelBars steps={emailFunnel(r.emails)} caption="Campaign email funnel" />
              </div>
              <p className="border-t border-border px-5 py-2 text-xs text-muted">
                {formatInt(r.emails.bounced)} bounced · {formatInt(r.emails.complained)} complaints · {formatInt(r.emails.unsubscribed)} unsubscribed
              </p>
            </Card>
          </div>
        </>
      ) : null}
      <div>
        <RangeFilter value={range} onChange={setRange} />
        <TimeseriesCard range={range} dimension={`campaign:${campaignId.toLowerCase()}`} title="Campaign trend" />
      </div>
    </div>
  );
}


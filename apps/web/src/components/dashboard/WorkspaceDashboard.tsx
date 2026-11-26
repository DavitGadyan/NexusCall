"use client";
/**
 * Workspace home dashboard: the last 30 UTC days at a glance (headline numbers and four daily trends).
 * Reuses the analytics API and chart components; shown only to members with `analytics:read`.
 */
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { Card, CardHeader, ErrorState, LoadingState } from "@/components/ui";
import { KpiTile, LineChart } from "@/components/analytics/charts";
import { presetRange } from "@/components/analytics/lib/dates";
import { formatCompact, formatDuration, formatMetricValue, formatPercent, formatUsd, metricAxisValue } from "@/components/analytics/lib/format";
import type { Overview, Timeseries } from "@/components/analytics/lib/types";

const TRENDS: { metric: string; title: string }[] = [
  { metric: "calls.answered", title: "Calls answered" },
  { metric: "bookings.created", title: "Appointments booked" },
  { metric: "emails.delivered", title: "Emails delivered" },
  { metric: "ai.cost_micros", title: "AI spend" },
];

export function WorkspaceDashboard() {
  const range = React.useMemo(() => presetRange(30), []);
  const overview = useApi<Overview>(withQuery("/api/v1/analytics/overview", range));
  const d = overview.data;
  return (
    <section aria-labelledby="dash-title" className="mb-6 flex flex-col gap-4">
      <h2 id="dash-title" className="text-sm font-semibold uppercase tracking-wide text-muted">
        Last 30 days
      </h2>
      {d ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="dashboard-kpis">
          <KpiTile label="Calls answered" value={formatCompact(d.calls.answered)} hint={`Answer rate ${formatPercent(d.calls.answer_rate)}`} />
          <KpiTile label="Qualified leads" value={formatCompact(d.calls.qualified)} hint={`Avg call ${formatDuration(d.calls.average_duration_seconds)}`} />
          <KpiTile label="Appointments" value={formatCompact(d.bookings.created)} hint={`${formatCompact(d.bookings.confirmed)} confirmed`} />
          <KpiTile label="AI replies sent" value={formatCompact(d.drafts.sent)} hint="Every one approved by a person" />
          <KpiTile label="Total cost" value={formatUsd(d.costs.total_cost_micros)} hint="Telephony, AI and email" />
        </div>
      ) : overview.error && !overview.loading ? (
        <Card>
          <ErrorState problem={overview.error.problem} onRetry={overview.reload} />
        </Card>
      ) : (
        <Card>
          <LoadingState label="Loading dashboard" />
        </Card>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        {TRENDS.map((t) => (
          <TrendCard key={t.metric} metric={t.metric} title={t.title} range={range} />
        ))}
      </div>
    </section>
  );
}

function TrendCard({ metric, title, range }: { metric: string; title: string; range: { from: string; to: string } }) {
  const ts = useApi<Timeseries>(withQuery("/api/v1/analytics/timeseries", { metric, from: range.from, to: range.to, dimension: "" }));
  const total = ts.data?.points.reduce((a, p) => a + p.value, 0) ?? 0;
  return (
    <Card>
      <CardHeader title={title} description={ts.data ? `${formatMetricValue(metric, total)} in 30 days` : "Per UTC day"} />
      <div className="px-5 py-4">
        {ts.data ? (
          <LineChart
            points={ts.data.points}
            label={title}
            format={(v) => formatMetricValue(metric, v)}
            axisFormat={(v) => metricAxisValue(metric, v)}
            stale={ts.loading}
          />
        ) : ts.error ? (
          <ErrorState problem={ts.error.problem} onRetry={ts.reload} />
        ) : (
          <LoadingState label={`Loading ${title.toLowerCase()}`} />
        )}
      </div>
    </Card>
  );
}

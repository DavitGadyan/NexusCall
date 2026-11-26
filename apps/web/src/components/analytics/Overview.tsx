"use client";
/**
 * Analytics overview for one UTC date range: headline numbers, the call / email / AI-draft / booking
 * funnels, costs and a daily time series. The range filter scopes every card below it.
 */
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { Card, CardHeader, ErrorState, LoadingState, Notice, Select } from "@/components/ui";
import { FunnelBars, KpiTile, LineChart } from "./charts";
import { RangeFilter } from "./RangeFilter";
import { type DateRange, formatUtcRange, presetRange } from "./lib/dates";
import { formatCompact, formatDuration, formatInt, formatMetricValue, formatPercent, formatUsd, metricAxisValue, metricLabel } from "./lib/format";
import { bookingFunnel, callFunnel, costParts, draftFunnel, emailFunnel, funnel } from "./lib/funnel";
import type { Overview as OverviewData, Timeseries } from "./lib/types";

export function Overview() {
  const [range, setRange] = React.useState<DateRange>(() => presetRange(30));
  const overview = useApi<OverviewData>(withQuery("/api/v1/analytics/overview", range));
  // useApi keeps the previous data while a new range loads: that frame stays, dimmed.
  const data = overview.data;
  const stale = overview.loading && data !== undefined;

  return (
    <div className="flex flex-col">
      <RangeFilter value={range} onChange={setRange} />
      <p className="mb-4 text-xs text-muted" aria-live="polite">
        {formatUtcRange(range)}. Days are UTC calendar days.
      </p>
      {overview.error && !overview.loading ? (
        <Card className="mb-4">
          <ErrorState problem={overview.error.problem} onRetry={overview.reload} />
        </Card>
      ) : null}
      {!data ? (
        overview.loading ? (
          <Card>
            <LoadingState label="Loading analytics" />
          </Card>
        ) : null
      ) : (
        <div className={stale ? "opacity-60 transition-opacity" : "transition-opacity"} aria-busy={stale}>
          <OverviewBody data={data} />
        </div>
      )}
      <TimeseriesCard range={range} />
    </div>
  );
}

function OverviewBody({ data }: { data: OverviewData }) {
  const c = data.calls;
  const e = data.emails;
  const d = data.drafts;
  const costs = data.costs;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="analytics-kpis">
        <KpiTile label="Total cost" value={formatUsd(costs.total_cost_micros)} hint={costs.email_cost_estimated ? "Includes estimated email cost" : undefined} />
        <KpiTile label="Calls answered" value={formatCompact(c.answered)} hint={`Answer rate ${formatPercent(c.answer_rate)}`} />
        <KpiTile label="Emails delivered" value={formatCompact(e.delivered)} hint={`Delivery rate ${formatPercent(e.delivery_rate)}`} />
        <KpiTile label="Bookings" value={formatCompact(data.bookings.created)} hint={`${formatInt(data.bookings.confirmed)} confirmed`} />
        <KpiTile label="Leads created" value={formatCompact(data.leads.created ?? 0)} hint={`${formatInt(data.campaigns.launched ?? 0)} campaigns launched`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Call funnel" description={`Average call ${formatDuration(c.average_duration_seconds)} · ${formatDuration(c.total_duration_seconds)} in total`} />
          <div className="py-3">
            <FunnelBars steps={callFunnel(c)} caption="Call funnel" />
          </div>
          <p className="border-t border-border px-5 py-2 text-xs text-muted">
            {formatInt(c.failed)} failed · {formatInt(c.canceled)} canceled · {formatInt(c.opted_out)} opted out
          </p>
        </Card>
        <Card>
          <CardHeader title="Email funnel" description={`Bounce rate ${formatPercent(e.bounce_rate, 2)} · complaint rate ${formatPercent(e.complaint_rate, 3)}`} />
          <div className="py-3">
            <FunnelBars steps={emailFunnel(e)} caption="Email funnel" />
          </div>
          <p className="border-t border-border px-5 py-2 text-xs text-muted">
            {formatInt(e.bounced)} bounced · {formatInt(e.complained)} complaints · {formatInt(e.rejected + e.failed)} rejected or failed · {formatInt(e.unsubscribed)} unsubscribed
          </p>
        </Card>
        <Card>
          <CardHeader
            title="AI draft outcomes"
            description={`Approved ${formatPercent(d.approved_rate)} · edited before approval ${formatPercent(d.edited_rate)} · rejected ${formatPercent(d.rejected_rate)}`}
          />
          <div className="py-3">
            <FunnelBars steps={draftFunnel(d)} caption="AI draft outcomes" />
          </div>
          <p className="border-t border-border px-5 py-2 text-xs text-muted">
            {formatInt(d.edited)} edited · {formatInt(d.rejected)} rejected. Every AI-written email is approved by a person before sending.
          </p>
        </Card>
        <Card>
          <CardHeader title="Bookings" description={`${formatInt(data.bookings.canceled)} canceled in this range`} />
          <div className="py-3">
            <FunnelBars steps={bookingFunnel(data.bookings)} caption="Bookings" />
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Costs" description="Provider and model costs attributed to this workspace (USD)." />
        <div className="grid gap-4 py-3 lg:grid-cols-[2fr_3fr]">
          <FunnelBars
            caption="Cost by channel"
            showShare={false}
            format={(v) => formatUsd(v)}
            steps={funnel(costParts(costs).map((p) => ({ key: p.key, label: p.label, value: p.value })))}
          />
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 px-5 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs text-muted">Per call</dt>
              <dd className="tabular">{formatUsd(costs.cost_per_call_micros, { precise: true })}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Per email</dt>
              <dd className="tabular">{formatUsd(costs.cost_per_email_micros, { precise: true })}{costs.email_cost_estimated ? " (est.)" : ""}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Per AI request</dt>
              <dd className="tabular">{formatUsd(costs.cost_per_ai_interaction_micros, { precise: true })}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">AI input tokens</dt>
              <dd className="tabular">{formatCompact(costs.ai_input_tokens)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">AI output tokens</dt>
              <dd className="tabular">{formatCompact(costs.ai_output_tokens)}</dd>
            </div>
          </dl>
        </div>
        {costs.email_cost_estimated ? (
          <div className="px-5 pb-4">
            <Notice tone="info">Email cost is estimated from message counts until provider billing data is available.</Notice>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

const DEFAULT_METRIC = "calls.answered";

export function TimeseriesCard({ range, dimension = "", title = "Daily trend" }: { range: DateRange; dimension?: string; title?: string }) {
  const metrics = useApi<{ items: string[] }>("/api/v1/analytics/metrics");
  const [metric, setMetric] = React.useState(DEFAULT_METRIC);
  const ts = useApi<Timeseries>(withQuery("/api/v1/analytics/timeseries", { metric, from: range.from, to: range.to, dimension }));
  // The previous series stays (dimmed) while the next loads, but only for the same metric.
  const data = ts.data?.metric === metric ? ts.data : undefined;
  const total = data?.points.reduce((a, p) => a + p.value, 0) ?? 0;
  const options = metrics.data?.items ?? [DEFAULT_METRIC];

  return (
    <Card className="mt-4">
      <CardHeader
        title={`${title}: ${metricLabel(metric)}`}
        description={data ? `${formatMetricValue(metric, total)} in total · per UTC day` : "Per UTC day"}
        actions={
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Metric</span>
            <Select value={metric} onChange={(e) => setMetric(e.target.value)} className="w-52" data-testid="metric-select">
              {options.map((m) => (
                <option key={m} value={m}>
                  {metricLabel(m)}
                </option>
              ))}
            </Select>
          </label>
        }
      />
      <div className="px-5 py-4">
        {data ? (
          <LineChart
            points={data.points}
            label={metricLabel(metric)}
            format={(v) => formatMetricValue(metric, v)}
            axisFormat={(v) => metricAxisValue(metric, v)}
            stale={ts.loading}
          />
        ) : ts.error ? (
          <ErrorState problem={ts.error.problem} onRetry={ts.reload} />
        ) : (
          <LoadingState label="Loading trend" />
        )}
      </div>
    </Card>
  );
}

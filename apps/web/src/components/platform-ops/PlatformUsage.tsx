"use client";
/**
 * Usage and cost per tenant for a UTC date range (analytics-service, read through its platform role).
 * The CSV download is built in the browser from the numbers already on screen.
 */
import { Download } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { downloadText } from "@/lib/client/download";
import { useApi } from "@/lib/client/use-api";
import { toCsv } from "@/lib/csv";
import { shortId } from "@/lib/format";
import type { Page, Tenant } from "@/lib/types";
import { Button, Card, CardHeader, EmptyState, ErrorState, LoadingState, Notice } from "@/components/ui";
import { KpiTile } from "@/components/analytics/charts";
import { RangeFilter } from "@/components/analytics/RangeFilter";
import { type DateRange, formatUtcRange, presetRange } from "@/components/analytics/lib/dates";
import { formatCompact, formatDuration, formatInt, formatUsd } from "@/components/analytics/lib/format";
import type { TenantUsage, UsageOut } from "@/components/analytics/lib/types";

type SortKey = "total_cost_micros" | "call_cost_micros" | "ai_cost_micros" | "email_cost_micros" | "calls" | "ai_requests";

export function PlatformUsage() {
  const [range, setRange] = React.useState<DateRange>(() => presetRange(30));
  const [sort, setSort] = React.useState<SortKey>("total_cost_micros");
  const usage = useApi<UsageOut>(withQuery("/api/v1/platform/usage", range));
  const tenants = useApi<Page<Tenant>>("/api/v1/platform/tenants");
  const names = React.useMemo(() => new Map((tenants.data?.items ?? []).map((t) => [t.id, t])), [tenants.data]);
  const u = usage.data;
  const rows = React.useMemo(() => [...(u?.items ?? [])].sort((a, b) => b[sort] - a[sort]), [u, sort]);
  const max = Math.max(1, ...rows.map((r) => r.total_cost_micros));

  function exportCsv() {
    if (!u) return;
    const header = ["tenant_id", "tenant", "calls", "call_seconds", "call_cost_usd", "emails_accepted", "email_cost_usd", "ai_requests", "ai_input_tokens", "ai_output_tokens", "ai_cost_usd", "total_cost_usd"];
    const usd = (m: number) => (m / 1_000_000).toFixed(6);
    const lines = rows.map((r) => [
      r.tenant_id,
      names.get(r.tenant_id)?.name ?? "",
      r.calls,
      r.call_seconds,
      usd(r.call_cost_micros),
      r.emails_accepted,
      usd(r.email_cost_micros),
      r.ai_requests,
      r.ai_input_tokens,
      r.ai_output_tokens,
      usd(r.ai_cost_micros),
      usd(r.total_cost_micros),
    ]);
    downloadText(`usage-${u.from}-to-${u.to}-utc.csv`, toCsv([header, ...lines]));
  }

  const sortHeader = (key: SortKey, label: string) => (
    <th scope="col" className="border-b border-border px-4 py-2 text-right" aria-sort={sort === key ? "descending" : "none"}>
      <button type="button" onClick={() => setSort(key)} className={sort === key ? "font-semibold text-fg" : "hover:text-fg"}>
        {label}
        {sort === key ? <span aria-hidden> ↓</span> : null}
      </button>
    </th>
  );

  return (
    <div className="flex flex-col">
      <RangeFilter value={range} onChange={setRange} />
      <p className="mb-4 text-xs text-muted">{formatUtcRange(range)}. Days are UTC calendar days.</p>
      {usage.loading && !u ? (
        <Card>
          <LoadingState label="Loading usage" />
        </Card>
      ) : usage.error && !u ? (
        <Card>
          <ErrorState problem={usage.error.problem} onRetry={usage.reload} />
        </Card>
      ) : u ? (
        <div className={usage.loading ? "flex flex-col gap-4 opacity-60 transition-opacity" : "flex flex-col gap-4 transition-opacity"}>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <KpiTile label="Total cost" value={formatUsd(u.totals.total_cost_micros)} hint={`${formatInt(u.totals.tenants)} tenants with usage`} />
            <KpiTile label="Call cost" value={formatUsd(u.totals.call_cost_micros)} hint={`${formatCompact(u.totals.calls)} calls · ${formatDuration(u.totals.call_seconds)}`} />
            <KpiTile label="AI cost" value={formatUsd(u.totals.ai_cost_micros)} hint={`${formatCompact(u.totals.ai_requests)} requests`} />
            <KpiTile label="Email cost" value={formatUsd(u.totals.email_cost_micros)} hint={`${formatCompact(u.totals.emails_accepted)} accepted${u.email_cost_estimated ? " · estimated" : ""}`} />
            <KpiTile label="Calls" value={formatCompact(u.totals.calls)} />
          </div>
          {u.email_cost_estimated ? <Notice tone="info">Email cost is estimated from message counts until provider billing data is available.</Notice> : null}
          <Card>
            <CardHeader
              title="Per tenant"
              description="Sorted by the highlighted column. The bar shows each tenant's total cost relative to the largest."
              actions={
                <Button variant="secondary" size="sm" onClick={exportCsv} disabled={rows.length === 0}>
                  <Download className="h-4 w-4" aria-hidden /> CSV
                </Button>
              }
            />
            {rows.length === 0 ? (
              <EmptyState title="No usage in this range" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Usage per tenant</caption>
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-muted">
                      <th scope="col" className="border-b border-border px-4 py-2">Tenant</th>
                      {sortHeader("calls", "Calls")}
                      {sortHeader("call_cost_micros", "Call cost")}
                      {sortHeader("ai_requests", "AI requests")}
                      {sortHeader("ai_cost_micros", "AI cost")}
                      {sortHeader("email_cost_micros", "Email cost")}
                      {sortHeader("total_cost_micros", "Total")}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <UsageRow key={r.tenant_id} r={r} tenant={names.get(r.tenant_id)} max={max} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      ) : null}
    </div>
  );
}

function UsageRow({ r, tenant, max }: { r: TenantUsage; tenant: Tenant | undefined; max: number }) {
  return (
    <tr className="hover:bg-surface-2/60">
      <td className="border-b border-border px-4 py-2">
        {tenant ? (
          <Link href={`/platform/tenants/${r.tenant_id}`} className="font-medium text-brand hover:underline">
            {tenant.name}
          </Link>
        ) : (
          <span className="font-mono text-xs">{shortId(r.tenant_id)}</span>
        )}
      </td>
      <td className="tabular border-b border-border px-4 py-2 text-right">
        {formatInt(r.calls)}
        <span className="block text-xs text-muted">{formatDuration(r.call_seconds)}</span>
      </td>
      <td className="tabular border-b border-border px-4 py-2 text-right">{formatUsd(r.call_cost_micros)}</td>
      <td className="tabular border-b border-border px-4 py-2 text-right">
        {formatInt(r.ai_requests)}
        <span className="block text-xs text-muted">{formatCompact(r.ai_input_tokens + r.ai_output_tokens)} tokens</span>
      </td>
      <td className="tabular border-b border-border px-4 py-2 text-right">{formatUsd(r.ai_cost_micros)}</td>
      <td className="tabular border-b border-border px-4 py-2 text-right">{formatUsd(r.email_cost_micros)}</td>
      <td className="border-b border-border px-4 py-2 text-right">
        <span className="tabular font-medium">{formatUsd(r.total_cost_micros)}</span>
        <span className="mt-1 block h-1 w-full rounded-full bg-brand/15" aria-hidden>
          <span className="block h-full rounded-full bg-brand" style={{ width: `${Math.max(1, (r.total_cost_micros / max) * 100)}%` }} />
        </span>
      </td>
    </tr>
  );
}

"use client";
/**
 * Platform overview: usage and cost across tenants for the last 30 UTC days (analytics-service
 * `/platform/usage`). Hidden when the viewer may not read usage.
 */
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import type { Page, Tenant } from "@/lib/types";
import { Card, CardHeader, LoadingState } from "@/components/ui";
import { FunnelBars, KpiTile } from "@/components/analytics/charts";
import { presetRange } from "@/components/analytics/lib/dates";
import { formatCompact, formatUsd } from "@/components/analytics/lib/format";
import type { FunnelStep } from "@/components/analytics/lib/funnel";
import type { UsageOut } from "@/components/analytics/lib/types";

const TOP = 8;

export function UsageSummary() {
  const range = React.useMemo(() => presetRange(30), []);
  const usage = useApi<UsageOut>(withQuery("/api/v1/platform/usage", range));
  const tenants = useApi<Page<Tenant>>("/api/v1/platform/tenants");
  const names = React.useMemo(() => new Map((tenants.data?.items ?? []).map((t) => [t.id, t.name])), [tenants.data]);
  const u = usage.data;
  if (usage.error && !u) return null; // no usage permission, or analytics unavailable: the rest of the page still works
  if (!u) {
    return (
      <Card>
        <LoadingState label="Loading platform usage" />
      </Card>
    );
  }
  const bars = (pick: (r: UsageOut["items"][number]) => number): FunnelStep[] =>
    [...u.items]
      .sort((a, b) => pick(b) - pick(a))
      .slice(0, TOP)
      .map((r) => ({ key: r.tenant_id, label: names.get(r.tenant_id) ?? r.tenant_id.slice(0, 8), value: pick(r), share: null }));
  const t = u.totals;
  const mix: FunnelStep[] = [
    { key: "calls", label: "Telephony", value: t.call_cost_micros, share: null },
    { key: "ai", label: "AI models", value: t.ai_cost_micros, share: null },
    { key: "email", label: "Email", value: t.email_cost_micros, share: null },
  ];
  return (
    <section aria-labelledby="usage-title" className="flex flex-col gap-4">
      <div className="flex items-baseline justify-between">
        <h2 id="usage-title" className="text-sm font-semibold uppercase tracking-wide text-muted">
          Platform usage · last 30 days
        </h2>
        <Link className="text-sm text-brand hover:underline" href="/platform/usage">
          Usage by tenant
        </Link>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <KpiTile label="Active tenants" value={formatCompact(t.tenants)} />
        <KpiTile label="Calls" value={formatCompact(t.calls)} hint={`${formatCompact(Math.round(t.call_seconds / 60))} minutes`} />
        <KpiTile label="Emails" value={formatCompact(t.emails_accepted)} />
        <KpiTile label="AI requests" value={formatCompact(t.ai_requests)} />
        <KpiTile label="Total cost" value={formatUsd(t.total_cost_micros)} hint={u.email_cost_estimated ? "Includes estimated email cost" : undefined} />
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Cost by tenant" description={`Top ${TOP}, telephony + AI + email`} />
          <FunnelBars steps={bars((r) => r.total_cost_micros)} caption="Cost by tenant" format={(v) => formatUsd(v)} showShare={false} />
        </Card>
        <Card>
          <CardHeader title="AI requests by tenant" description="Through the AI gateway" />
          <FunnelBars steps={bars((r) => r.ai_requests)} caption="AI requests by tenant" format={(v) => formatCompact(v)} showShare={false} />
        </Card>
        <Card>
          <CardHeader title="Cost mix" description="Where the platform spends" />
          <FunnelBars steps={mix} caption="Cost mix" format={(v) => formatUsd(v)} showShare={false} />
        </Card>
      </div>
    </section>
  );
}

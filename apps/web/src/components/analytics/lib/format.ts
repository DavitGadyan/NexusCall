/** Number formatting for analytics and usage. Costs arrive in micro-USD (1 USD = 1,000,000 micros). */

export function formatUsd(micros: number | null | undefined, opts: { precise?: boolean } = {}): string {
  if (micros === null || micros === undefined || !Number.isFinite(micros)) return "—";
  const usd = micros / 1_000_000;
  if (usd !== 0 && Math.abs(usd) < 0.01 && !opts.precise) return usd < 0 ? "−<$0.01" : "<$0.01";
  const digits = opts.precise && Math.abs(usd) < 1 ? 4 : 2;
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(usd);
}

export function formatPercent(rate: number | null | undefined, digits = 1): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(digits)}%`;
}

export function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

/** 1,284 / 12.9K / 4.2M — for stat tiles and axis ticks that reach 10,000. */
export function formatCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 10_000) return formatInt(n);
  if (abs < 1_000_000) return `${trim(n / 1_000)}K`;
  if (abs < 1_000_000_000) return `${trim(n / 1_000_000)}M`;
  return `${trim(n / 1_000_000_000)}B`;
}

function trim(v: number): string {
  return (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)).replace(/\.0$/, "");
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  return rest ? `${m}m ${rest}s` : `${m}m`;
}

/** Formats a metric value by its unit suffix (`*.cost_micros`, `*.duration_seconds`, `*.latency_ms`). */
export function formatMetricValue(metric: string, value: number): string {
  if (metric.endsWith("cost_micros")) return formatUsd(value);
  if (metric.endsWith("duration_seconds")) return formatDuration(value);
  if (metric.endsWith("latency_ms")) return `${formatInt(value)} ms`;
  return formatInt(value);
}

export function metricAxisValue(metric: string, value: number): string {
  if (metric.endsWith("cost_micros")) {
    const usd = value / 1_000_000;
    return usd >= 10_000 ? `$${formatCompact(usd)}` : `$${usd.toLocaleString("en-US", { maximumFractionDigits: usd < 10 ? 2 : 0 })}`;
  }
  if (metric.endsWith("duration_seconds")) return formatDuration(value);
  return formatCompact(value);
}

const METRIC_LABELS: Record<string, string> = {
  "calls.attempted": "Calls attempted",
  "calls.answered": "Calls answered",
  "calls.ended": "Calls ended",
  "calls.completed": "Calls completed",
  "calls.failed": "Calls failed",
  "calls.canceled": "Calls canceled",
  "calls.opted_out": "Opt-outs on calls",
  "calls.qualified": "Qualified calls",
  "calls.duration_seconds": "Call time",
  "calls.cost_micros": "Call cost",
  "bookings.created": "Bookings created",
  "bookings.confirmed": "Bookings confirmed",
  "bookings.canceled": "Bookings canceled",
  "emails.accepted": "Emails accepted",
  "emails.delivered": "Emails delivered",
  "emails.bounced": "Emails bounced",
  "emails.complained": "Spam complaints",
  "emails.rejected": "Emails rejected",
  "emails.failed": "Emails failed",
  "emails.unsubscribed": "Unsubscribes",
  "drafts.requested": "AI drafts requested",
  "drafts.generated": "AI drafts generated",
  "drafts.approved": "AI drafts approved",
  "drafts.edited": "AI drafts edited",
  "drafts.rejected": "AI drafts rejected",
  "drafts.sent": "AI drafts sent",
  "ai.requests": "AI requests",
  "ai.input_tokens": "AI input tokens",
  "ai.output_tokens": "AI output tokens",
  "ai.cost_micros": "AI cost",
  "ai.latency_ms": "AI latency (sum)",
  "leads.created": "Leads created",
  "campaigns.launched": "Campaigns launched",
};

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric.replace(/[._]/g, " ");
}

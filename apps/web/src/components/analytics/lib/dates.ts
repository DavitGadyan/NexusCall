/**
 * Analytics days are UTC calendar days (analytics_service buckets events by `occurred_at` in UTC), so every
 * range and label here is computed and shown in UTC, never in the viewer's zone.
 */
export const MAX_RANGE_DAYS = 366;
const DAY_MS = 86_400_000;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

export type DateRange = { from: string; to: string };

export function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function utcToday(now = Date.now()): string {
  return isoDay(now);
}

/** The last `days` UTC days, ending today (inclusive), like the service's default of 30. */
export function presetRange(days: number, now = Date.now()): DateRange {
  const end = Date.parse(`${utcToday(now)}T00:00:00Z`);
  return { from: isoDay(end - (days - 1) * DAY_MS), to: isoDay(end) };
}

export function parseDay(value: string): number | null {
  if (!ISO_DAY.test(value)) return null;
  const t = Date.parse(`${value}T00:00:00Z`);
  if (Number.isNaN(t) || isoDay(t) !== value) return null;
  return t;
}

export function rangeDays(r: DateRange): number | null {
  const a = parseDay(r.from);
  const b = parseDay(r.to);
  if (a === null || b === null) return null;
  return Math.round((b - a) / DAY_MS) + 1;
}

export function validateRange(r: DateRange, max = MAX_RANGE_DAYS): string | null {
  const a = parseDay(r.from);
  const b = parseDay(r.to);
  if (a === null || b === null) return "Enter both dates.";
  if (a > b) return "The start date must not be after the end date.";
  const days = Math.round((b - a) / DAY_MS) + 1;
  if (days > max) return `The range may span at most ${max} days.`;
  return null;
}

const SHORT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const LONG = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

export function formatUtcDay(value: string, opts: { year?: boolean } = {}): string {
  const t = parseDay(value);
  if (t === null) return "—";
  return (opts.year ? LONG : SHORT).format(new Date(t));
}

export function formatUtcRange(r: DateRange): string {
  return `${formatUtcDay(r.from, { year: true })} – ${formatUtcDay(r.to, { year: true })} (UTC)`;
}

export const PRESETS = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
] as const;

/** Which preset (if any) a range equals, for highlighting the active one. */
export function matchPreset(r: DateRange, now = Date.now()): number | null {
  for (const p of PRESETS) {
    const pr = presetRange(p.days, now);
    if (pr.from === r.from && pr.to === r.to) return p.days;
  }
  return null;
}

/** Current billing period (YYYY-MM, UTC) and the previous few, for AI usage. */
export function recentPeriods(count: number, now = Date.now()): string[] {
  const d = new Date(now);
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const y = d.getUTCFullYear();
    const m = d.getUTCMonth() - i;
    const date = new Date(Date.UTC(y, m, 1));
    out.push(`${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

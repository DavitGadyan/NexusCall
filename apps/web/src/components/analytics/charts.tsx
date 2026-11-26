"use client";
/**
 * Analytics charts, built to the dataviz skill's rules:
 * - one series per chart, so one hue (the brand token, validated against both surfaces) and no legend box;
 *   the card title names the series
 * - 2px line with a ~10% area wash, hairline solid gridlines, clean y ticks, x labels as UTC days
 * - a crosshair that snaps to the nearest day with a tooltip (value first, then the day), the same on
 *   keyboard focus (arrow keys), and a table view, so a tooltip never gates a value
 * - funnels and cost parts are ranked bars in one hue with the value at the tip, rendered as tables
 * - refetches keep the previous frame at reduced opacity (no skeleton flash)
 */
import * as React from "react";
import { cn } from "@/lib/cn";
import { areaPath, type Geometry, labelIndexes, linePath, nearestIndex, niceTicks, xAt, yAt } from "./lib/chart";
import { formatUtcDay } from "./lib/dates";
import { formatPercent } from "./lib/format";
import type { FunnelStep } from "./lib/funnel";
import type { Point } from "./lib/types";

const PLOT_HEIGHT = 200;
const AXIS_BAND = 28;

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = React.useRef<T | null>(null);
  const [width, setWidth] = React.useState(640);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && Math.abs(w - width) > 1) setWidth(Math.round(w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [width]);
  return [ref, width];
}

export function LineChart({
  points,
  label,
  format,
  axisFormat,
  stale,
}: {
  points: Point[];
  /** what is plotted, e.g. "Calls answered" (used for the accessible name and the table caption) */
  label: string;
  format: (v: number) => string;
  axisFormat: (v: number) => string;
  /** a refetch is in flight: keep this frame, dimmed */
  stale?: boolean;
}) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = React.useState<number | null>(null);
  const [showTable, setShowTable] = React.useState(false);
  const values = points.map((p) => p.value);
  const ticks = niceTicks(Math.max(0, ...values));
  const top = ticks[ticks.length - 1] ?? 1;
  const g: Geometry = { width, height: PLOT_HEIGHT + AXIS_BAND, padLeft: 52, padRight: 12, padTop: 10, padBottom: AXIS_BAND };
  const xLabels = labelIndexes(points.length, width < 480 ? 3 : 6);
  const ap = active !== null ? points[active] : undefined;

  function onPointer(e: React.PointerEvent<SVGSVGElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    setActive(nearestIndex(e.clientX - rect.left, points.length, g));
  }

  function onKey(e: React.KeyboardEvent) {
    if (points.length === 0) return;
    const cur = active ?? points.length - 1;
    let next = cur;
    if (e.key === "ArrowRight") next = Math.min(points.length - 1, cur + 1);
    else if (e.key === "ArrowLeft") next = Math.max(0, cur - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = points.length - 1;
    else if (e.key === "Escape") {
      setActive(null);
      return;
    } else return;
    e.preventDefault();
    setActive(next);
  }

  if (points.length === 0) return <p className="px-5 py-10 text-center text-sm text-muted">No data for this range.</p>;

  const ax = ap && active !== null ? xAt(active, points.length, g) : 0;
  const ay = ap ? yAt(ap.value, top, g) : 0;

  return (
    <div className="flex flex-col gap-2">
      <div
        ref={wrapRef}
        className={cn("relative w-full transition-opacity focus:outline-none focus-visible:ring-2 focus-visible:ring-brand", stale && "opacity-60")}
        tabIndex={0}
        role="group"
        aria-label={`${label} per UTC day. Use the left and right arrow keys to read each day.`}
        onKeyDown={onKey}
        onFocus={() => setActive((a) => a ?? points.length - 1)}
        onBlur={() => setActive(null)}
      >
        <svg width={width} height={g.height} className="block" onPointerMove={onPointer} onPointerLeave={() => setActive(null)} aria-hidden>
          {ticks.map((t) => {
            const y = yAt(t, top, g);
            return (
              <g key={t}>
                <line x1={g.padLeft} x2={width - g.padRight} y1={y} y2={y} stroke="rgb(var(--border))" strokeWidth={1} shapeRendering="crispEdges" />
                <text x={g.padLeft - 8} y={y} dy="0.32em" textAnchor="end" className="tabular fill-[rgb(var(--muted))] text-[11px]">
                  {axisFormat(t)}
                </text>
              </g>
            );
          })}
          {xLabels.map((i) => {
            const p = points[i];
            if (!p) return null;
            const anchor = i === 0 ? "start" : i === points.length - 1 ? "end" : "middle";
            return (
              <text key={p.day} x={xAt(i, points.length, g)} y={g.height - 8} textAnchor={anchor} className="fill-[rgb(var(--muted))] text-[11px]">
                {formatUtcDay(p.day)}
              </text>
            );
          })}
          <path d={areaPath(values, top, g)} fill="rgb(var(--brand))" fillOpacity={0.1} />
          <path d={linePath(values, top, g)} fill="none" stroke="rgb(var(--brand))" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          {values.length === 1 ? <circle cx={xAt(0, 1, g)} cy={yAt(values[0] ?? 0, top, g)} r={4} fill="rgb(var(--brand))" /> : null}
          {ap ? (
            <g>
              <line x1={ax} x2={ax} y1={g.padTop} y2={g.height - g.padBottom} stroke="rgb(var(--muted))" strokeOpacity={0.5} strokeWidth={1} />
              <circle cx={ax} cy={ay} r={5} fill="rgb(var(--brand))" stroke="rgb(var(--surface))" strokeWidth={2} />
            </g>
          ) : null}
        </svg>
        {ap ? (
          <div
            role="status"
            aria-live="polite"
            className="pointer-events-none absolute top-1 z-10 min-w-[8rem] rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-md"
            style={ax > width / 2 ? { right: width - ax + 10 } : { left: ax + 10 }}
          >
            <p className="flex items-center gap-2">
              <span aria-hidden className="inline-block h-0.5 w-3 rounded bg-brand" />
              <span className="text-sm font-semibold text-fg">{format(ap.value)}</span>
            </p>
            <p className="mt-0.5 text-muted">{formatUtcDay(ap.day, { year: true })} (UTC)</p>
          </div>
        ) : null}
      </div>
      <div className="flex justify-end">
        <button type="button" onClick={() => setShowTable((s) => !s)} aria-expanded={showTable} className="text-xs font-medium text-brand hover:underline">
          {showTable ? "Hide table" : "Show as table"}
        </button>
      </div>
      {showTable ? (
        <div className="max-h-72 overflow-y-auto rounded-md border border-border">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">{label} per UTC day</caption>
            <thead className="sticky top-0 bg-surface">
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th scope="col" className="border-b border-border px-3 py-1.5">Day (UTC)</th>
                <th scope="col" className="border-b border-border px-3 py-1.5 text-right">{label}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.day}>
                  <td className="border-b border-border px-3 py-1">{formatUtcDay(p.day, { year: true })}</td>
                  <td className="tabular border-b border-border px-3 py-1 text-right">{format(p.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Ranked bars in one hue. Bars are capped at 12px thick with a rounded data end and a square baseline;
 * the count sits at the tip in text ink, the share of the first step beside it in muted ink.
 */
export function FunnelBars({ steps, caption, format = (v) => v.toLocaleString("en-US"), showShare = true }: { steps: FunnelStep[]; caption: string; format?: (v: number) => string; showShare?: boolean }) {
  const max = Math.max(1, ...steps.map((s) => s.value));
  if (steps.every((s) => s.value === 0)) return <p className="px-5 py-6 text-sm text-muted">Nothing in this range yet.</p>;
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">{caption}</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Step</th>
          <th scope="col">Bar</th>
          <th scope="col">Count</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((s) => (
          <tr key={s.key} className="group" title={`${s.label}: ${format(s.value)}${showShare && s.share !== null ? ` (${formatPercent(s.share, 0)} of ${steps[0]?.label.toLowerCase()})` : ""}`}>
            <th scope="row" className="w-28 whitespace-nowrap py-1.5 pl-5 pr-3 text-left font-normal">
              {s.label}
            </th>
            <td className="py-1.5 pr-3" aria-hidden>
              <div className="h-3 w-full">
                <div
                  className="h-full rounded-r-[4px] bg-brand/85 transition-colors group-hover:bg-brand"
                  style={{ width: s.value > 0 ? `${Math.max(1, (s.value / max) * 100)}%` : "0%" }}
                />
              </div>
            </td>
            <td className="tabular w-32 whitespace-nowrap py-1.5 pr-5 text-right">
              {format(s.value)}
              {showShare && s.share !== null ? <span className="ml-1.5 text-xs text-muted">{formatPercent(s.share, 0)}</span> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * A single ratio against a limit. The fill carries severity (brand → warn at 80% → danger at 100%); the
 * track is a lighter step of the same hue. The numbers are always printed beside it.
 */
export function Meter({ value, limit, label, format }: { value: number; limit: number; label: string; format: (v: number) => string }) {
  const ratio = limit > 0 ? value / limit : 0;
  const pct = Math.min(100, Math.max(0, ratio * 100));
  const tone = ratio >= 1 ? "danger" : ratio >= 0.8 ? "warn" : "brand";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="text-muted">{label}</span>
        <span>
          <span className="font-semibold">{format(value)}</span> <span className="text-muted">of {format(limit)} ({Math.round(ratio * 100)}%)</span>
        </span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.min(value, limit)}
        aria-valuetext={`${format(value)} of ${format(limit)}`}
        className={cn("h-2 w-full overflow-hidden rounded-full", tone === "danger" ? "bg-danger/15" : tone === "warn" ? "bg-warn/15" : "bg-brand/15")}
      >
        <div className={cn("h-full rounded-full", tone === "danger" ? "bg-danger" : tone === "warn" ? "bg-warn" : "bg-brand")} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/** Headline number: proportional figures (no tabular-nums), sentence-case label, optional hint. */
export function KpiTile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-5 py-4">
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

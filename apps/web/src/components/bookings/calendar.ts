/**
 * Pure calendar math for the bookings week/day views and the availability-rule editor (unit-tested).
 * Times are displayed in the viewer's local time zone; the API exchanges ISO instants.
 */
import type { ApiError } from "@/lib/client/api";
import type { Booking, Rule } from "./types";

export const DAY_MS = 86_400_000;
export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;

export function startOfDay(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

export function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

/** Monday 00:00 (local) of the week containing `d`. */
export function startOfWeek(d: Date): Date {
  const x = startOfDay(d);
  const offset = (x.getDay() + 6) % 7; // Monday = 0
  return addDays(x, -offset);
}

export type View = "week" | "day";

export function viewRange(view: View, anchor: Date): { from: Date; to: Date; days: Date[] } {
  const from = view === "week" ? startOfWeek(anchor) : startOfDay(anchor);
  const count = view === "week" ? 7 : 1;
  const days = Array.from({ length: count }, (_, i) => addDays(from, i));
  return { from, to: addDays(from, count), days };
}

export function shiftAnchor(view: View, anchor: Date, direction: -1 | 1): Date {
  return addDays(anchor, direction * (view === "week" ? 7 : 1));
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export type Placed = { booking: Booking; startMin: number; endMin: number; lane: number; lanes: number };

/**
 * Bookings that overlap `day`, as minute offsets from local midnight (clipped to the day), with side-by-side
 * lanes for overlaps (different resources, or canceled bookings next to their replacement).
 */
export function layoutDay(bookings: readonly Booking[], day: Date): Placed[] {
  const dayStart = startOfDay(day).getTime();
  const dayEnd = addDays(startOfDay(day), 1).getTime();
  const items = bookings
    .map((b) => ({ b, s: new Date(b.start).getTime(), e: new Date(b.end).getTime() }))
    .filter((x) => !Number.isNaN(x.s) && !Number.isNaN(x.e) && x.e > dayStart && x.s < dayEnd)
    .sort((a, b) => a.s - b.s || b.e - a.e);
  const out: Placed[] = [];
  let cluster: Placed[] = [];
  let clusterEnd = -Infinity;
  const laneEnds: number[] = [];
  const flush = () => {
    const lanes = Math.max(1, ...cluster.map((p) => p.lane + 1));
    for (const p of cluster) p.lanes = lanes;
    out.push(...cluster);
    cluster = [];
    laneEnds.length = 0;
  };
  for (const x of items) {
    const startMin = Math.max(0, Math.round((x.s - dayStart) / 60_000));
    const endMin = Math.min(1440, Math.max(startMin + 1, Math.round((x.e - dayStart) / 60_000)));
    if (startMin >= clusterEnd && cluster.length) flush();
    let lane = laneEnds.findIndex((end) => end <= startMin);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(endMin);
    } else laneEnds[lane] = endMin;
    cluster.push({ booking: x.b, startMin, endMin, lane, lanes: 1 });
    clusterEnd = Math.max(clusterEnd === -Infinity ? endMin : clusterEnd, endMin);
  }
  if (cluster.length) flush();
  return out;
}

/** Visible hour range: 07–20 by default, widened to include every booking shown. */
export function hourBounds(placed: readonly Placed[], minHour = 7, maxHour = 20): { start: number; end: number } {
  let start = minHour;
  let end = maxHour;
  for (const p of placed) {
    start = Math.min(start, Math.floor(p.startMin / 60));
    end = Math.max(end, Math.ceil(p.endMin / 60));
  }
  return { start: Math.max(0, start), end: Math.min(24, end) };
}

/** ISO instant -> value for <input type="datetime-local"> in local time. */
export function toLocalInput(iso: string | Date): string {
  const d = typeof iso === "string" ? new Date(iso) : iso;
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** <input type="datetime-local"> value (local time) -> ISO instant with an explicit offset (UTC). */
export function fromLocalInput(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function toDateInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function fromDateInput(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatTime(iso: string | Date): string {
  const d = typeof iso === "string" ? new Date(iso) : iso;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(d);
}

export function formatRange(startIso: string, endIso: string): string {
  const s = new Date(startIso);
  const e = new Date(endIso);
  const day = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(s);
  return `${day}, ${formatTime(s)} – ${formatTime(e)}`;
}

export type BookingConflict = "slot_taken" | "stale" | "state";

/**
 * booking-service answers 409 for three different reasons. A taken slot (exclusion constraint or hold) is
 * the default; optimistic-lock and state-machine conflicts are recognised from the problem detail.
 */
export function classifyBookingConflict(err: Pick<ApiError, "status" | "message">): BookingConflict | null {
  if (err.status !== 409) return null;
  const m = err.message.toLowerCase();
  if (m.includes("changed by someone else") || m.includes("reload")) return "stale";
  if (m.includes("cannot become") || m.includes("cannot be rescheduled") || m.includes("outcome can only") || m.includes("hold has expired")) return "state";
  return "slot_taken";
}

/** "HH:MM[:SS]" -> minutes since midnight, or null. */
export function parseTime(value: string): number | null {
  const m = /^(\d{2}):(\d{2})(?::\d{2})?$/.exec(value);
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h > 23 || min > 59) return null;
  return h * 60 + min;
}

/** Mirrors booking-service RuleIn and the same-weekday overlap check. Keys are `rules.<i>.<field>`. */
export function validateRules(rules: readonly Rule[]): Record<string, string> {
  const errors: Record<string, string> = {};
  const byDay = new Map<number, { i: number; s: number; e: number }[]>();
  rules.forEach((r, i) => {
    const s = parseTime(r.start_time);
    const e = parseTime(r.end_time);
    if (s === null) errors[`rules.${i}.start_time`] = "Use HH:MM.";
    if (e === null) errors[`rules.${i}.end_time`] = "Use HH:MM.";
    if (s !== null && e !== null && e <= s) errors[`rules.${i}.end_time`] = "Must be after the start.";
    if (!Number.isInteger(r.slot_minutes) || r.slot_minutes < 5 || r.slot_minutes > 480) errors[`rules.${i}.slot_minutes`] = "5 to 480 minutes.";
    if (!Number.isInteger(r.buffer_before_minutes) || r.buffer_before_minutes < 0 || r.buffer_before_minutes > 240) errors[`rules.${i}.buffer_before_minutes`] = "0 to 240 minutes.";
    if (!Number.isInteger(r.buffer_after_minutes) || r.buffer_after_minutes < 0 || r.buffer_after_minutes > 240) errors[`rules.${i}.buffer_after_minutes`] = "0 to 240 minutes.";
    if (!Number.isInteger(r.min_notice_minutes) || r.min_notice_minutes < 0 || r.min_notice_minutes > 20160) errors[`rules.${i}.min_notice_minutes`] = "0 to 20160 minutes (14 days).";
    if (s !== null && e !== null && e > s) {
      const list = byDay.get(r.weekday) ?? [];
      list.push({ i, s, e });
      byDay.set(r.weekday, list);
    }
  });
  for (const list of byDay.values()) {
    list.sort((a, b) => a.s - b.s);
    for (let k = 1; k < list.length; k++) {
      const prev = list[k - 1]!;
      const cur = list[k]!;
      if (cur.s < prev.e) errors[`rules.${cur.i}.start_time`] = "Overlaps another window on the same day.";
    }
  }
  return errors;
}

/** API times come back as "HH:MM:SS"; inputs use "HH:MM". */
export function trimSeconds(t: string): string {
  return /^\d{2}:\d{2}:\d{2}$/.test(t) ? t.slice(0, 5) : t;
}

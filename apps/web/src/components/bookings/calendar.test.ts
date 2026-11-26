import { describe, expect, it } from "vitest";
import { classifyBookingConflict, fromDateInput, fromLocalInput, hourBounds, layoutDay, parseTime, shiftAnchor, startOfWeek, toLocalInput, trimSeconds, validateRules, viewRange } from "./calendar";
import type { Booking, Rule } from "./types";

const local = (d: number, h: number, m = 0) => new Date(2026, 0, d, h, m); // Jan 2026, local time
const booking = (id: string, start: Date, end: Date, status = "confirmed"): Booking => ({
  id,
  contact_id: "c",
  resource_id: "r",
  start: start.toISOString(),
  end: end.toISOString(),
  status,
  source_type: "manual",
  cancel_reason: null,
  hold_expires_at: null,
  calendar_sync_state: null,
  external_event_id: null,
  version: 1,
  created_at: start.toISOString(),
  updated_at: start.toISOString(),
});

describe("week and day ranges", () => {
  it("starts weeks on Monday", () => {
    // 2026-01-08 is a Thursday
    const w = startOfWeek(local(8, 15));
    expect(w.getDay()).toBe(1);
    expect(w.getDate()).toBe(5);
    expect(w.getHours()).toBe(0);
  });
  it("Sunday belongs to the week that started the Monday before", () => {
    expect(startOfWeek(local(11, 9)).getDate()).toBe(5);
  });
  it("builds 7 or 1 day ranges", () => {
    const w = viewRange("week", local(8, 15));
    expect(w.days).toHaveLength(7);
    expect(w.to.getDate()).toBe(12);
    const d = viewRange("day", local(8, 15));
    expect(d.days).toHaveLength(1);
    expect(d.from.getDate()).toBe(8);
    expect(d.to.getDate()).toBe(9);
  });
  it("shifts by a week or a day", () => {
    expect(shiftAnchor("week", local(8, 0), 1).getDate()).toBe(15);
    expect(shiftAnchor("day", local(8, 0), -1).getDate()).toBe(7);
  });
});

describe("layoutDay", () => {
  it("places bookings in minutes from midnight", () => {
    const [p] = layoutDay([booking("a", local(8, 9, 30), local(8, 10))], local(8, 0));
    expect(p).toMatchObject({ startMin: 570, endMin: 600, lane: 0, lanes: 1 });
  });
  it("puts overlapping bookings side by side and keeps separate clusters at one lane", () => {
    const placed = layoutDay(
      [booking("a", local(8, 9), local(8, 10)), booking("b", local(8, 9, 30), local(8, 10, 30)), booking("c", local(8, 13), local(8, 14))],
      local(8, 0),
    );
    const byId = Object.fromEntries(placed.map((p) => [p.booking.id, p]));
    expect(byId.a).toMatchObject({ lane: 0, lanes: 2 });
    expect(byId.b).toMatchObject({ lane: 1, lanes: 2 });
    expect(byId.c).toMatchObject({ lane: 0, lanes: 1 });
  });
  it("reuses a free lane inside a cluster", () => {
    const placed = layoutDay(
      [booking("a", local(8, 9), local(8, 12)), booking("b", local(8, 9), local(8, 10)), booking("c", local(8, 10), local(8, 11))],
      local(8, 0),
    );
    const byId = Object.fromEntries(placed.map((p) => [p.booking.id, p]));
    expect(byId.c?.lane).toBe(1);
    expect(byId.a?.lanes).toBe(2);
  });
  it("clips bookings that cross midnight and ignores other days", () => {
    const placed = layoutDay([booking("a", local(7, 23), local(8, 1)), booking("b", local(9, 9), local(9, 10))], local(8, 0));
    expect(placed).toHaveLength(1);
    expect(placed[0]).toMatchObject({ startMin: 0, endMin: 60 });
  });
  it("widens the visible hours to include early or late bookings", () => {
    const placed = layoutDay([booking("a", local(8, 6, 15), local(8, 7)), booking("b", local(8, 20, 30), local(8, 21, 15))], local(8, 0));
    expect(hourBounds(placed)).toEqual({ start: 6, end: 22 });
    expect(hourBounds([])).toEqual({ start: 7, end: 20 });
  });
});

describe("date inputs", () => {
  it("round-trips datetime-local values through ISO", () => {
    const iso = fromLocalInput("2026-01-08T09:30");
    expect(iso).not.toBeNull();
    expect(toLocalInput(iso!)).toBe("2026-01-08T09:30");
  });
  it("rejects malformed values", () => {
    expect(fromLocalInput("2026-01-08 09:30")).toBeNull();
    expect(fromLocalInput("")).toBeNull();
    expect(fromDateInput("08/01/2026")).toBeNull();
    expect(fromDateInput("2026-01-08")?.getDate()).toBe(8);
  });
});

describe("classifyBookingConflict", () => {
  const err = (status: number, message: string) => ({ status, message });
  it("treats an unavailable time as a taken slot", () => {
    expect(classifyBookingConflict(err(409, "The requested time is no longer available"))).toBe("slot_taken");
  });
  it("recognises optimistic-lock and state conflicts", () => {
    expect(classifyBookingConflict(err(409, "The booking was changed by someone else; reload and retry"))).toBe("stale");
    expect(classifyBookingConflict(err(409, "A canceled booking cannot be rescheduled"))).toBe("state");
    expect(classifyBookingConflict(err(409, "The tentative hold has expired; book a new slot"))).toBe("state");
  });
  it("ignores other statuses", () => {
    expect(classifyBookingConflict(err(422, "bad"))).toBeNull();
  });
});

describe("validateRules", () => {
  const rule = (over: Partial<Rule> = {}): Rule => ({ weekday: 0, start_time: "09:00", end_time: "12:00", slot_minutes: 30, buffer_before_minutes: 0, buffer_after_minutes: 0, min_notice_minutes: 60, ...over });
  it("accepts non-overlapping windows", () => {
    expect(validateRules([rule(), rule({ start_time: "13:00", end_time: "17:00" }), rule({ weekday: 1, start_time: "10:00", end_time: "11:00" })])).toEqual({});
  });
  it("flags overlaps on the same weekday only", () => {
    const e = validateRules([rule(), rule({ start_time: "11:30", end_time: "14:00" }), rule({ weekday: 2, start_time: "11:30", end_time: "14:00" })]);
    expect(Object.keys(e)).toEqual(["rules.1.start_time"]);
  });
  it("checks time order and bounds", () => {
    const e = validateRules([rule({ start_time: "12:00", end_time: "09:00", slot_minutes: 3, buffer_after_minutes: 300, min_notice_minutes: -1 })]);
    expect(Object.keys(e).sort()).toEqual(["rules.0.buffer_after_minutes", "rules.0.end_time", "rules.0.min_notice_minutes", "rules.0.slot_minutes"]);
    expect(validateRules([rule({ start_time: "9am" })])["rules.0.start_time"]).toBe("Use HH:MM.");
  });
  it("parses and trims times", () => {
    expect(parseTime("09:30:00")).toBe(570);
    expect(parseTime("24:00")).toBeNull();
    expect(trimSeconds("09:30:00")).toBe("09:30");
    expect(trimSeconds("09:30")).toBe("09:30");
  });
});

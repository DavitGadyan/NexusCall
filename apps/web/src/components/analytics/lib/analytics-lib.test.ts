import { describe, expect, it } from "vitest";
import { labelIndexes, nearestIndex, niceTicks, xAt, yAt } from "./chart";
import { formatUtcDay, matchPreset, presetRange, rangeDays, recentPeriods, validateRange } from "./dates";
import { formatCompact, formatDuration, formatMetricValue, formatPercent, formatUsd } from "./format";
import { callFunnel, costParts, funnel } from "./funnel";

const NOW = Date.parse("2026-03-10T23:30:00Z");

describe("UTC dates", () => {
  it("builds presets from the UTC day, not the local one", () => {
    expect(presetRange(7, NOW)).toEqual({ from: "2026-03-04", to: "2026-03-10" });
    expect(presetRange(1, NOW)).toEqual({ from: "2026-03-10", to: "2026-03-10" });
    expect(matchPreset({ from: "2026-02-09", to: "2026-03-10" }, NOW)).toBe(30);
    expect(matchPreset({ from: "2026-02-08", to: "2026-03-10" }, NOW)).toBeNull();
  });

  it("validates ranges like the service (inclusive, at most 366 days)", () => {
    expect(validateRange({ from: "2026-01-01", to: "2026-01-01" })).toBeNull();
    expect(validateRange({ from: "2026-01-02", to: "2026-01-01" })).toMatch(/after/);
    expect(validateRange({ from: "2025-01-01", to: "2026-01-01" })).toBeNull(); // 366 days inclusive
    expect(validateRange({ from: "2024-12-31", to: "2026-01-01" })).toMatch(/366/);
    expect(validateRange({ from: "2026-02-30", to: "2026-03-01" })).toMatch(/both/);
    expect(rangeDays({ from: "2026-03-01", to: "2026-03-31" })).toBe(31);
  });

  it("labels days in UTC", () => {
    expect(formatUtcDay("2026-03-01")).toBe("Mar 1");
    expect(formatUtcDay("2026-03-01", { year: true })).toBe("Mar 1, 2026");
    expect(formatUtcDay("bad")).toBe("—");
  });

  it("lists billing periods across a year boundary", () => {
    expect(recentPeriods(3, Date.parse("2026-01-15T00:00:00Z"))).toEqual(["2026-01", "2025-12", "2025-11"]);
  });
});

describe("formatting", () => {
  it("formats micro-dollars", () => {
    expect(formatUsd(1_234_560_000)).toBe("$1,234.56");
    expect(formatUsd(4_000)).toBe("<$0.01");
    expect(formatUsd(4_000, { precise: true })).toBe("$0.0040");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(null)).toBe("—");
  });
  it("formats rates, counts and durations", () => {
    expect(formatPercent(0.4567)).toBe("45.7%");
    expect(formatPercent(null)).toBe("—");
    expect(formatCompact(1284)).toBe("1,284");
    expect(formatCompact(12_900)).toBe("12.9K");
    expect(formatCompact(4_200_000)).toBe("4.2M");
    expect(formatDuration(59)).toBe("59s");
    expect(formatDuration(125)).toBe("2m 5s");
    expect(formatDuration(3720)).toBe("1h 2m");
    expect(formatMetricValue("calls.cost_micros", 2_500_000)).toBe("$2.50");
    expect(formatMetricValue("calls.answered", 1200)).toBe("1,200");
  });
});

describe("funnels", () => {
  it("computes the share of the first step", () => {
    const steps = callFunnel({
      attempted: 200, answered: 100, completed: 80, qualified: 20, ended: 0, failed: 0, canceled: 0, opted_out: 0,
      total_duration_seconds: 0, average_duration_seconds: null, answer_rate: null,
    });
    expect(steps.map((s) => s.share)).toEqual([1, 0.5, 0.4, 0.1]);
  });
  it("has no share when the first step is zero", () => {
    expect(funnel([{ key: "a", label: "A", value: 0 }, { key: "b", label: "B", value: 3 }]).map((s) => s.share)).toEqual([null, null]);
  });
  it("ranks cost parts largest first", () => {
    expect(costParts({ ai_cost_micros: 5, call_cost_micros: 9, email_cost_micros: 1 }).map((p) => p.key)).toEqual(["calls", "ai", "email"]);
  });
});

describe("chart geometry", () => {
  const g = { width: 600, height: 200, padLeft: 40, padRight: 10, padTop: 10, padBottom: 30 };
  it("produces clean ticks covering the max", () => {
    expect(niceTicks(97)).toEqual([0, 25, 50, 75, 100]);
    expect(niceTicks(8)).toEqual([0, 2, 4, 6, 8]);
    expect(niceTicks(0)).toEqual([0, 1]);
    expect(niceTicks(1234).at(-1)).toBeGreaterThanOrEqual(1234);
  });
  it("maps values and snaps the pointer to the nearest day", () => {
    expect(xAt(0, 5, g)).toBe(40);
    expect(xAt(4, 5, g)).toBe(590);
    expect(yAt(0, 100, g)).toBe(170);
    expect(yAt(100, 100, g)).toBe(10);
    expect(nearestIndex(40, 5, g)).toBe(0);
    expect(nearestIndex(330, 5, g)).toBe(2);
    expect(nearestIndex(9999, 5, g)).toBe(4);
  });
  it("picks evenly spaced axis labels including both ends", () => {
    expect(labelIndexes(30, 6)).toEqual([0, 6, 12, 17, 23, 29]);
    expect(labelIndexes(3, 6)).toEqual([0, 1, 2]);
  });
});

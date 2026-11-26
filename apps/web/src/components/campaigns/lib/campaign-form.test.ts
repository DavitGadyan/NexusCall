import { describe, expect, it } from "vitest";
import {
  actionsFor,
  breakdown,
  buildCampaign,
  type Campaign,
  campaignPatch,
  campaignToForm,
  defaultForm,
  editableFields,
  limitProblems,
  problemLabel,
  problemsOf,
} from "./campaign-form";

const campaign: Campaign = {
  id: "c", name: "Spring", description: null, state: "draft", agent_id: "a", segment_id: "s", timezone: "UTC",
  window_start: "09:00", window_end: "17:00", window_days: ["mon", "tue"], calls_per_minute: 10, max_concurrency: 5,
  max_attempts: 3, retry_delay_seconds: 3600, start_at: null, end_at: null, launched_at: null, completed_at: null,
  failure_reason: null, snapshot_status: "none", snapshot_total: 0, version: 4, created_at: "", updated_at: "",
};

describe("campaign form", () => {
  it("builds a CampaignIn body from the defaults", () => {
    const r = buildCampaign({ ...defaultForm("Europe/Berlin"), name: " Spring " });
    expect(r.errors).toEqual({});
    expect(r.body).toMatchObject({ name: "Spring", timezone: "Europe/Berlin", window_days: ["mon", "tue", "wed", "thu", "fri"], retry_delay_seconds: 3600, agent_id: null, start_at: null });
  });

  it("validates ranges, the window and the date order", () => {
    const r = buildCampaign({ ...defaultForm(), name: "", calls_per_minute: "601", max_concurrency: "0", window_start: "18:00", window_end: "09:00", window_days: [], start_at: "2026-05-02T10:00", end_at: "2026-05-01T10:00" });
    expect(Object.keys(r.errors).sort()).toEqual(["calls_per_minute", "end_at", "max_concurrency", "name", "window_days", "window_end"].sort());
    expect(buildCampaign({ ...defaultForm(), name: "x", max_attempts: "2.5" }).errors.max_attempts).toMatch(/whole number/);
  });

  it("round-trips a stored campaign and patches only what changed", () => {
    const form = campaignToForm(campaign);
    expect(campaignPatch(campaign, buildCampaign(form).body!)).toEqual({});
    const edited = buildCampaign({ ...form, calls_per_minute: "20", window_days: ["tue", "mon", "fri"] }).body!;
    expect(campaignPatch(campaign, edited)).toEqual({ calls_per_minute: 20, window_days: ["mon", "tue", "fri"], expected_version: 4 });
  });

  it("limits edits by state", () => {
    expect(editableFields("draft")).toBe("all");
    expect(editableFields("running")).toBe("none");
    const paused = { ...campaign, state: "paused" };
    const body = buildCampaign({ ...campaignToForm(paused), name: "Renamed", calls_per_minute: "12" }).body!;
    expect(campaignPatch(paused, body)).toEqual({ calls_per_minute: 12, expected_version: 4 });
  });

  it("mirrors the tenant limit checks", () => {
    const body = buildCampaign({ ...defaultForm(), name: "x", max_concurrency: "50", window_start: "07:00", window_days: ["sat"] }).body!;
    expect(limitProblems(body, null)).toEqual([]);
    expect(limitProblems(body, { max_concurrent_calls: 10, max_daily_calls: 1000, calls_enabled: false, calling_window: { start: "08:00", end: "20:00", days: ["mon", "tue", "wed", "thu", "fri"] } })).toEqual([
      "calls_disabled_for_tenant",
      "max_concurrency_exceeds_tenant_limit",
      "window_outside_tenant_calling_window",
      "days_outside_tenant_calling_window",
    ]);
  });

  it("labels problems and reads them from a problem body", () => {
    expect(problemLabel("agent_not_active")).toMatch(/Activate a version/);
    expect(problemLabel("something_new")).toBe("something new");
    expect(problemsOf({ problems: ["agent_required", 3] })).toEqual(["agent_required"]);
    expect(problemsOf({})).toEqual([]);
  });

  it("derives lifecycle actions", () => {
    expect(actionsFor({ state: "scheduled" })).toEqual(["cancel", "launch", "unschedule"]);
    expect(actionsFor({ state: "running", allowed_actions: ["pause", "complete", "cancel"] })).toEqual(["pause", "cancel"]);
    expect(actionsFor({ state: "completed" })).toEqual([]);
  });

  it("computes report breakdowns", () => {
    expect(breakdown({ failed: 1, completed: 3 })).toEqual([{ key: "completed", count: 3, share: 0.75 }, { key: "failed", count: 1, share: 0.25 }]);
    expect(breakdown({})).toEqual([]);
  });
});

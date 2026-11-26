import { describe, expect, it } from "vitest";
import { validateNumberForm } from "./number-form";
import { buildTimeline, callStateTone, contactLabel, formatDuration, isLive, isUuid, liveElapsedSeconds, safeHttpUrl, secondsLeft } from "./shared";
import type { Call, VoiceSession } from "./types";

const call = (over: Partial<Call> = {}): Call => ({
  id: "6f1d3a52-6a0a-4a4c-9f5e-1b2c3d4e5f60",
  state: "completed",
  direction: "outbound",
  is_test: false,
  agent_id: null,
  campaign_id: null,
  contact_id: null,
  inbound_number_id: null,
  from_masked: null,
  to_masked: "+1•••••0123",
  attempt_count: 1,
  blocked_reason: null,
  end_reason: "hangup",
  handoff_state: null,
  opted_out: false,
  cost_micros: 0,
  duration_seconds: 65,
  scheduled_for: null,
  initiated_at: "2026-01-05T10:00:00Z",
  started_at: "2026-01-05T10:00:02Z",
  answered_at: "2026-01-05T10:00:10Z",
  ended_at: "2026-01-05T10:01:15Z",
  created_at: "2026-01-05T09:59:59Z",
  updated_at: "2026-01-05T10:01:15Z",
  ...over,
});

describe("formatDuration", () => {
  it("formats minutes and hours", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3723)).toBe("1:02:03");
  });
  it("renders missing or invalid values as a dash", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(-1)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("live state helpers", () => {
  it("knows the live states of the call state machine", () => {
    expect(isLive("ringing")).toBe(true);
    expect(isLive("in_progress")).toBe(true);
    expect(isLive("queued")).toBe(false);
    expect(isLive("completed")).toBe(false);
  });
  it("maps states to tones", () => {
    expect(callStateTone("in_progress")).toBe("ok");
    expect(callStateTone("failed")).toBe("danger");
    expect(callStateTone("whatever")).toBe("neutral");
  });
  it("measures talk time from answer, else from dialing", () => {
    const now = new Date("2026-01-05T10:00:40Z").getTime();
    expect(liveElapsedSeconds(call(), now)).toBe(30);
    expect(liveElapsedSeconds(call({ answered_at: null, started_at: null }), now)).toBe(40);
    expect(liveElapsedSeconds(call({ answered_at: "2026-01-05T11:00:00Z" }), now)).toBe(0);
  });
});

describe("buildTimeline", () => {
  it("orders lifecycle events and includes handoffs", () => {
    const session = {
      started_at: "2026-01-05T10:00:11Z",
      ended_at: "2026-01-05T10:01:15Z",
      end_reason: null,
      handoffs: [{ trigger: "caller_request", status: "connected", created_at: "2026-01-05T10:00:50Z" }],
    } as unknown as VoiceSession;
    const t = buildTimeline(call(), session);
    expect(t.map((e) => e.label)).toEqual([
      "Call created",
      "Dialing started",
      "Provider call started",
      "Answered",
      "AI voice session started",
      "Handoff to a person",
      "Ended (completed)",
    ]);
    expect(t[5]?.detail).toBe("caller request — connected");
  });
  it("skips missing timestamps", () => {
    const t = buildTimeline(call({ initiated_at: null, started_at: null, answered_at: null, ended_at: null, state: "queued" }));
    expect(t.map((e) => e.label)).toEqual(["Call created"]);
  });
});

describe("safeHttpUrl", () => {
  it("accepts absolute http(s) URLs only", () => {
    expect(safeHttpUrl("https://minio.local/rec.wav?X-Amz-Signature=abc")).toContain("https://minio.local/rec.wav");
    expect(safeHttpUrl("http://localhost:9000/a")).toBe("http://localhost:9000/a");
  });
  it("refuses script, data, relative and non-string values", () => {
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,<script>")).toBeNull();
    expect(safeHttpUrl("/relative/path")).toBeNull();
    expect(safeHttpUrl("memory://bucket/key")).toBeNull();
    expect(safeHttpUrl(42)).toBeNull();
    expect(safeHttpUrl(`https://x.test/${"a".repeat(9000)}`)).toBeNull();
  });
});

describe("secondsLeft", () => {
  it("counts down and never goes negative", () => {
    expect(secondsLeft(0, 900, 0)).toBe(900);
    expect(secondsLeft(0, 900, 899_500)).toBe(1);
    expect(secondsLeft(0, 900, 2_000_000)).toBe(0);
  });
});

describe("misc", () => {
  it("labels contacts", () => {
    expect(contactLabel({ id: "abcdef123456", first_name: "Ana", last_name: "Lopez", email: null })).toBe("Ana Lopez");
    expect(contactLabel({ id: "abcdef123456", first_name: null, last_name: null, email: "a@x.test" })).toBe("a@x.test");
    expect(contactLabel({ id: "abcdef123456", first_name: null, last_name: null, email: null })).toBe("Contact abcdef12");
  });
  it("checks UUIDs", () => {
    expect(isUuid("6f1d3a52-6a0a-4a4c-9f5e-1b2c3d4e5f60")).toBe(true);
    expect(isUuid("../etc")).toBe(false);
    expect(isUuid(undefined)).toBe(false);
  });
});

describe("validateNumberForm", () => {
  const base = { e164: "+14155550123", label: "Main", agent_id: "a", business_hours_only: false, handoff_e164: "", provider_number_sid: "", use_for_outbound: true };
  it("accepts a valid number", () => {
    expect(validateNumberForm(base, true)).toEqual({});
  });
  it("rejects bad E.164, missing agent, bad handoff and bad SID", () => {
    const e = validateNumberForm({ ...base, e164: "4155550123", agent_id: "", handoff_e164: "123", provider_number_sid: "PNxyz" }, true);
    expect(Object.keys(e).sort()).toEqual(["agent_id", "e164", "handoff_e164", "provider_number_sid"]);
  });
  it("does not re-validate immutable fields when editing", () => {
    expect(validateNumberForm({ ...base, e164: "bad", provider_number_sid: "bad" }, false)).toEqual({});
  });
});

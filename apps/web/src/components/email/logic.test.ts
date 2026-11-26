import { describe, expect, it } from "vitest";
import { campaignActions, dnsLabel, dnsTone, percent, reportRates, reportRows, reportSegments, scheduleBody, senderBlockReason, templateProblems } from "./logic";

describe("DNS and sender status", () => {
  it("maps statuses to tones and labels", () => {
    expect(dnsTone("pass")).toBe("ok");
    expect(dnsTone("verified")).toBe("ok");
    expect(dnsTone("fail")).toBe("danger");
    expect(dnsTone("pending")).toBe("warn");
    expect(dnsLabel("pending")).toBe("Not checked yet");
  });
  it("blocks senders whose domain is not verified or that are disabled", () => {
    expect(senderBlockReason({ status: "active", domain_status: "verified" })).toBeNull();
    expect(senderBlockReason({ status: "active", domain_status: "pending" })).toBe("Domain not verified");
    expect(senderBlockReason({ status: "disabled", domain_status: "verified" })).toBe("Sender is disabled");
  });
});

describe("campaignActions", () => {
  it("follows the campaign state machine", () => {
    expect(campaignActions("draft")).toEqual({ edit: true, schedule: true, pause: false, resume: false, cancel: true });
    expect(campaignActions("running")).toMatchObject({ edit: false, schedule: false, pause: true, resume: false, cancel: true });
    expect(campaignActions("paused")).toMatchObject({ pause: false, resume: true, cancel: true });
    expect(campaignActions("completed")).toEqual({ edit: false, schedule: false, pause: false, resume: false, cancel: false });
  });
});

describe("delivery report", () => {
  const report = { counts: { delivered: 80, bounced: 5, accepted: 10, queued: 5, mystery: 0 }, recipients: 100 };
  it("lists every known state in pipeline order with shares, plus unknown ones", () => {
    const rows = reportRows(report);
    expect(rows[0]?.state).toBe("pending");
    expect(rows.find((r) => r.state === "delivered")).toMatchObject({ count: 80, share: 0.8 });
    expect(rows.find((r) => r.state === "failed")).toMatchObject({ count: 0, share: 0 });
    expect(rows.at(-1)?.state).toBe("mystery");
  });
  it("folds into three outcome segments with in-flight as the remainder", () => {
    const bar = reportSegments(reportRows(report));
    expect(bar.total).toBe(100);
    expect(bar.inFlight).toBe(5);
    expect(bar.segments.map((s) => [s.key, s.count])).toEqual([
      ["delivered", 80],
      ["accepted", 10],
      ["problem", 5],
    ]);
  });
  it("handles an empty report", () => {
    const bar = reportSegments(reportRows({ counts: {}, recipients: 0 }));
    expect(bar.total).toBe(0);
    expect(bar.segments.every((s) => s.share === 0)).toBe(true);
  });
  it("never shows open or click rates", () => {
    const rates = reportRates({ delivery_rate: 0.9, bounce_rate: 0.05, open_rate: 0.4, click_through: 0.1 });
    expect(rates.map((r) => r.key)).toEqual(["delivery_rate", "bounce_rate"]);
    expect(rates[0]?.label).toBe("Delivered");
  });
  it("formats percentages", () => {
    expect(percent(0.1234)).toBe("12.3%");
    expect(percent(0.0005, 2)).toBe("0.05%");
  });
});

describe("scheduleBody", () => {
  it("sends an ISO instant, nothing for 'now', and null for junk", () => {
    expect(scheduleBody("")).toEqual({});
    expect(scheduleBody("2026-03-01T09:00")).toEqual({ scheduled_at: new Date("2026-03-01T09:00").toISOString() });
    expect(scheduleBody("not a date")).toBeNull();
  });
});

describe("templateProblems", () => {
  it("extracts the server's problem list", () => {
    expect(templateProblems({ problems: ["Unknown variable {{ x }}", 3, "Link is not https"] })).toEqual(["Unknown variable {{ x }}", "Link is not https"]);
    expect(templateProblems(undefined)).toEqual([]);
    expect(templateProblems({ problems: "nope" })).toEqual([]);
  });
});

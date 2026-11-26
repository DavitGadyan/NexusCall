import { describe, expect, it } from "vitest";
import { argText, contextLinks, effectiveStatus, executionMessage, riskLabel, toolLabel } from "./approvals";

const ID = "6f1d3a52-6a0a-4a4c-9f5e-1b2c3d4e5f60";

describe("tool approval display", () => {
  it("labels tools and risk", () => {
    expect(toolLabel("crm.update_contact")).toBe("Crm › Update contact");
    expect(toolLabel("booking:create-hold")).toBe("Booking › Create hold");
    expect(riskLabel(1).tone).toBe("neutral");
    expect(riskLabel(2).tone).toBe("warn");
    expect(riskLabel(4)).toEqual({ label: "High risk (class 4)", tone: "danger" });
  });
  it("renders arguments as text, truncating long values", () => {
    expect(argText("hello")).toBe("hello");
    expect(argText(3)).toBe("3");
    expect(argText(null)).toBe("—");
    expect(argText({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(argText("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>");
    expect(argText("x".repeat(50), 10)).toBe(`${"x".repeat(10)}…`);
  });
  it("links only well-formed ids of known context kinds", () => {
    const links = contextLinks({ contact_id: ID, call_id: "not-a-uuid", channel: "voice", empty: "", n: 4 });
    expect(links).toEqual([
      { label: "Contact", value: ID, href: `/app/crm/contacts/${ID}` },
      { label: "Call", value: "not-a-uuid", href: null },
      { label: "channel", value: "voice", href: null },
    ]);
  });
  it("treats a pending request past its deadline as expired", () => {
    const now = new Date("2026-01-01T12:00:00Z").getTime();
    expect(effectiveStatus({ status: "pending", expires_at: "2026-01-01T11:59:59Z" }, now)).toBe("expired");
    expect(effectiveStatus({ status: "pending", expires_at: "2026-01-01T12:05:00Z" }, now)).toBe("pending");
    expect(effectiveStatus({ status: "approved", expires_at: "2026-01-01T11:00:00Z" }, now)).toBe("approved");
  });
  it("summarises the execution result", () => {
    const approval = {} as never;
    expect(executionMessage({ approval, execution: { status: "executed" } }).tone).toBe("success");
    expect(executionMessage({ approval, execution: { status: "denied", reason: "grant revoked" } })).toMatchObject({ tone: "error", description: "grant revoked" });
    expect(executionMessage({ approval, execution: null }).title).toBe("Approved");
  });
});

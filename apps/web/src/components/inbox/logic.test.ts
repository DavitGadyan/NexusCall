import { describe, expect, it } from "vitest";
import { actionLabel, approveBlockReason, assigneeLabel, attachmentState, canEdit, canEscalate, canReject, confidence, currentVersion, draftFlags, formatBytes, replySubject } from "./logic";
import type { DraftVersion } from "./types";

const v = (version: number, quality: DraftVersion["quality"] = {}): DraftVersion => ({
  version,
  author_type: "ai",
  author_id: "ai-agent-service",
  ai_generated: true,
  body_text: `text ${version}`,
  citations: [],
  quality,
  model: "m",
  provider: "p",
  prompt_version: 1,
  created_at: "2026-01-01T00:00:00Z",
});

describe("draftFlags", () => {
  it("explains ungrounded and sensitive flags, most serious first", () => {
    const flags = draftFlags(v(1, { flags: ["needs_human", "ungrounded"] }), { sensitive: true });
    expect(flags.map((f) => f.flag)).toEqual(["sensitive", "ungrounded", "needs_human"]);
    expect(flags[0]?.tone).toBe("danger");
    expect(flags[1]?.label).toBe("Not grounded");
  });
  it("keeps unknown flags visible and ignores non-strings", () => {
    const flags = draftFlags(v(1, { flags: ["weird_flag", 7 as unknown as string] }));
    expect(flags).toHaveLength(1);
    expect(flags[0]).toMatchObject({ flag: "weird_flag", label: "weird flag", tone: "warn" });
  });
  it("copes with missing quality data", () => {
    expect(draftFlags(null)).toEqual([]);
    expect(draftFlags(v(1, { flags: "x" as unknown as string[] }))).toEqual([]);
  });
  it("clamps confidence", () => {
    expect(confidence(v(1, { confidence: 0.42 }))).toBe(0.42);
    expect(confidence(v(1, { confidence: 3 }))).toBe(1);
    expect(confidence(v(1, {}))).toBeNull();
  });
});

describe("approval rules", () => {
  const d = { status: "pending_review", current_version: 3 };
  it("allows only the current version of a draft waiting for review", () => {
    expect(approveBlockReason(d, 3)).toBeNull();
    expect(approveBlockReason(d, 2)).toBe("Only the latest version can be approved.");
    expect(approveBlockReason({ ...d, status: "approved" }, 3)).toMatch(/cannot be approved/);
    expect(approveBlockReason({ ...d, current_version: 0 }, 0)).toBe("There is no draft text yet.");
  });
  it("blocks approval while edits are unsaved", () => {
    expect(approveBlockReason(d, 3, { unsavedEdits: true })).toBe("Save or discard your edits first.");
  });
  it("mirrors which statuses can be edited, rejected and escalated", () => {
    expect(canEdit("pending_review")).toBe(true);
    expect(canEdit("sending")).toBe(false);
    expect(canReject("queued")).toBe(true);
    expect(canReject("sent")).toBe(false);
    expect(canEscalate("sent")).toBe(false);
    expect(canEscalate("generating")).toBe(true);
  });
  it("finds the current version", () => {
    expect(currentVersion({ current_version: 2, versions: [v(1), v(2)] })?.body_text).toBe("text 2");
    expect(currentVersion({ current_version: 0, versions: [] })).toBeNull();
  });
});

describe("attachmentState", () => {
  const base = { downloadable: true, quarantined: false, scan_status: "clean", rejection_reason: null };
  it("offers download only for clean, downloadable files", () => {
    expect(attachmentState(base).kind).toBe("download");
    expect(attachmentState({ ...base, scan_status: "skipped" })).toMatchObject({ kind: "download", tone: "warn" });
  });
  it("never offers an infected or quarantined file", () => {
    expect(attachmentState({ ...base, scan_status: "infected", downloadable: true })).toMatchObject({ kind: "blocked", tone: "danger" });
    expect(attachmentState({ ...base, quarantined: true })).toMatchObject({ kind: "blocked" });
  });
  it("explains other unavailable states", () => {
    expect(attachmentState({ ...base, scan_status: "error", downloadable: false }).kind).toBe("unavailable");
    expect(attachmentState({ ...base, scan_status: "purged", downloadable: false }).label).toMatch(/retention/);
    expect(attachmentState({ ...base, scan_status: "rejected", rejection_reason: "too_large", downloadable: false }).label).toBe("Rejected: too large");
    expect(attachmentState({ ...base, downloadable: false }).kind).toBe("unavailable");
  });
});

describe("formatting", () => {
  it("builds reply subjects like the server", () => {
    expect(replySubject("Pricing")).toBe("Re: Pricing");
    expect(replySubject("RE: Pricing")).toBe("RE: Pricing");
  });
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
    expect(formatBytes(-1)).toBe("—");
  });
  it("labels actions and assignees", () => {
    expect(actionLabel("approved")).toBe("Approved for sending");
    expect(actionLabel("custom_thing")).toBe("custom thing");
    expect(assigneeLabel(null, "me")).toBe("Unassigned");
    expect(assigneeLabel("me", "me")).toBe("You");
    expect(assigneeLabel("1234567890ab", "me")).toBe("12345678…");
  });
});

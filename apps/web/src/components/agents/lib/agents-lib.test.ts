import { describe, expect, it } from "vitest";
import { compareVersions, diffLines, diffStats, withContext } from "./diff";
import { approvalForced, canGrant, draftsFromGrants, grantsBody, sameGrants, setApproval, setRate, toggleGrant } from "./grants";
import { buildVersion, emptyVersionForm, mergeErrors, versionToForm } from "./version-form";
import type { AgentVersion, Tool } from "./types";

const tool = (id: string, risk: number, over: Partial<Tool> = {}): Tool => ({
  tool_id: id, display_name: id, description: "", kind: "internal", risk_class: risk, side_effecting: risk >= 2,
  requires_approval: false, enabled: true, platform_enabled: true, rate_per_minute: 60, reason: null, version: 1, input_schema: {}, ...over,
});

describe("line diff", () => {
  it("finds added and removed lines", () => {
    const lines = diffLines("a\nb\nc\nd", "a\nB\nc\nd\ne");
    expect(lines.map((l) => `${l.kind}:${l.text}`)).toEqual(["same:a", "del:b", "add:B", "same:c", "same:d", "add:e"]);
    expect(diffStats(lines)).toEqual({ added: 2, removed: 1 });
  });

  it("handles identical and empty texts", () => {
    expect(diffLines("x\ny", "x\ny").every((l) => l.kind === "same")).toBe(true);
    expect(diffLines("", "new").map((l) => l.kind)).toEqual(["del", "add"]);
  });

  it("collapses unchanged runs", () => {
    const before = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n");
    const after = before.replace("line 10", "LINE 10");
    const out = withContext(diffLines(before, after), 2);
    expect(out[0]).toEqual({ kind: "gap", count: 8 });
    expect(out.filter((l) => l.kind !== "gap")).toHaveLength(6);
    expect(out[out.length - 1]).toEqual({ kind: "gap", count: 7 });
  });
});

const version = (over: Partial<AgentVersion>): AgentVersion => ({
  id: "v", agent_id: "a", version: 1, system_prompt: "You are a helpful scheduling assistant.", greeting: "Hi", disclosure_text: "", change_note: "",
  model: null, temperature: null, voice_id: null, max_call_seconds: 600, tool_ids: ["knowledge.search"], knowledge_source_ids: [],
  escalation_rules: [], created_by: "u", created_at: "", ...over,
});

describe("version comparison", () => {
  it("diffs prompt text and lists changed settings", () => {
    const cmp = compareVersions(version({}), version({ version: 2, greeting: "Hello", temperature: 0.2, tool_ids: ["knowledge.search", "crm.add_note"] }));
    expect(cmp.text.map((t) => t.field)).toEqual(["greeting"]);
    expect(cmp.settings).toEqual([
      { label: "Temperature", before: "—", after: "0.2" },
      { label: "Tools", before: "knowledge.search", after: "knowledge.search, crm.add_note" },
    ]);
  });

  it("ignores list order", () => {
    expect(compareVersions(version({ tool_ids: ["a", "b"] }), version({ tool_ids: ["b", "a"] })).settings).toEqual([]);
  });
});

describe("version form", () => {
  it("validates VersionIn rules", () => {
    const r = buildVersion({ ...emptyVersionForm(), system_prompt: "short", model: "Bad Model", temperature: "1.5", max_call_seconds: "10" }, "voice");
    expect(Object.keys(r.errors).sort()).toEqual(["max_call_seconds", "model", "system_prompt", "temperature"]);
  });

  it("restricts tools by agent kind and requires escalation keywords", () => {
    const base = { ...emptyVersionForm(), system_prompt: "x".repeat(25) };
    expect(buildVersion({ ...base, tool_ids: ["booking.confirm"] }, "voice").errors.tool_ids).toMatch(/booking.confirm/);
    expect(buildVersion({ ...base, tool_ids: ["knowledge.search"] }, "email").errors.tool_ids).toMatch(/email/);
    expect(buildVersion({ ...base, escalation_rules: [{ trigger: "keyword", action: "end_call", keyword: "" }] }, "voice").errors["escalation_rules.0.keyword"]).toBeTruthy();
    const ok = buildVersion({ ...base, temperature: "0.3", tool_ids: ["crm.add_note", "knowledge.search", "crm.add_note"], escalation_rules: [{ trigger: "complaint", action: "transfer_to_human", keyword: "ignored" }] }, "voice");
    expect(ok.body).toMatchObject({ temperature: 0.3, model: null, tool_ids: ["crm.add_note", "knowledge.search"], escalation_rules: [{ trigger: "complaint", action: "transfer_to_human", keyword: null }] });
  });

  it("copies an existing version without its change note", () => {
    const f = versionToForm(version({ change_note: "old", temperature: 0.5 }));
    expect(f).toMatchObject({ change_note: "", temperature: "0.5", tool_ids: ["knowledge.search"] });
    expect(mergeErrors({ name: "local" }, { "config.system_prompt": "api", name: "api" }, "config.")).toEqual({ system_prompt: "api", name: "local" });
  });
});

describe("tool grants", () => {
  const read = tool("crm.get_contact", 1);
  const ext = tool("booking.confirm", 3);
  const priv = tool("iam.manage_users", 4);
  const policy = tool("crm.add_note", 2, { requires_approval: true });

  it("never grants class 4 and forces approval for class 3", () => {
    expect(canGrant(priv)).toBe(false);
    expect(approvalForced(ext)).toBe(true);
    expect(approvalForced(policy)).toBe(true);
    let d = toggleGrant(new Map(), priv, true);
    expect(d.size).toBe(0);
    d = toggleGrant(d, ext, true);
    expect(d.get(ext.tool_id)).toEqual({ tool_id: ext.tool_id, requires_approval: true, calls_per_minute: null });
    d = setApproval(d, ext, false);
    expect(d.get(ext.tool_id)?.requires_approval).toBe(true);
  });

  it("edits approval and rate and builds a sorted PUT body", () => {
    let d = toggleGrant(new Map(), read, true);
    d = setApproval(d, read, true);
    const bad = setRate(d, read.tool_id, "0");
    expect(bad.error).toBeTruthy();
    d = setRate(d, read.tool_id, "30").drafts;
    d = toggleGrant(d, ext, true);
    // A class 4 entry slipped in from stale data is dropped from the body.
    d.set(priv.tool_id, { tool_id: priv.tool_id, requires_approval: false, calls_per_minute: null });
    expect(grantsBody(d, [read, ext, priv])).toEqual({
      grants: [
        { tool_id: "booking.confirm", requires_approval: true, calls_per_minute: null },
        { tool_id: "crm.get_contact", requires_approval: true, calls_per_minute: 30 },
      ],
    });
  });

  it("detects unsaved changes", () => {
    const saved = draftsFromGrants([{ tool_id: "a.b", risk_class: 1, requires_approval: false, effective_requires_approval: false, calls_per_minute: null, usable: true, reason: null }]);
    expect(sameGrants(saved, new Map(saved))).toBe(true);
    expect(sameGrants(saved, toggleGrant(saved, read, true))).toBe(false);
  });
});

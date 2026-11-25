import { describe, expect, it } from "vitest";
import { errorsByField, parseList, PLATFORM_SETTINGS, sectionPayload, TENANT_SETTINGS } from "./settings-descriptors";

describe("settings descriptors", () => {
  it("cover every tenant and platform section of tenant-service", () => {
    expect(TENANT_SETTINGS.map((s) => s.id).sort()).toEqual(["ai", "approval", "branding", "communication", "modules", "profile", "retention"]);
    expect(PLATFORM_SETTINGS.map((s) => s.id).sort()).toEqual(["branding", "kill_switches", "limits", "maintenance", "modules", "retention", "security"]);
  });

  it("marks the high-risk platform sections the API protects with step-up", () => {
    expect(PLATFORM_SETTINGS.filter((s) => s.highRisk).map((s) => s.id).sort()).toEqual(["kill_switches", "limits", "maintenance", "security"]);
  });

  it("shows platform-locked controls as locked", () => {
    const locked = TENANT_SETTINGS.flatMap((s) => s.fields.filter((f) => f.locked).map((f) => `${s.id}.${f.key}`));
    expect(locked).toEqual(
      expect.arrayContaining([
        "communication.honor_opt_out_across_channels",
        "ai.draft_auto_send",
        "ai.store_prompt_content",
        "approval.require_approval_for_ai_email",
      ]),
    );
  });

  it("strips metadata keys from the payload", () => {
    expect(sectionPayload({ name: "Acme", _version: 3 })).toEqual({ name: "Acme" });
  });

  it("rolls nested errors up to their field", () => {
    expect(errorsByField({ "calling_window.start": "expected HH:MM", "calling_window._root": "start before end", name: "too short" })).toEqual({
      calling_window: "expected HH:MM; start before end",
      name: "too short",
    });
  });

  it("parses comma lists", () => {
    expect(parseList("us, ca  gb,", true)).toEqual(["US", "CA", "GB"]);
    expect(parseList("")).toEqual([]);
  });
});

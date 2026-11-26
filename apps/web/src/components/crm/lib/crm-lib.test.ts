import { describe, expect, it } from "vitest";
import { buildActivity, reasonLabel } from "./activity";
import { buildContact, coerceCustom, contactPatch, contactToForm, emptyContactForm } from "./contact-form";
import { buildImportOptions, previewCsv } from "./csv-preview";
import { formatBytes, isVersionConflict, nextPollDelay, safeDownloadUrl, validateUploadFile } from "./http";
import { type Contact, contactName, type FieldDef } from "./types";

const field = (over: Partial<FieldDef>): FieldDef => ({ id: "f", key: "k", label: "K", type: "text", options: [], required: false, version: 1, created_at: "", updated_at: "", ...over });

describe("upload and download helpers", () => {
  it("validates extension, emptiness and size", () => {
    expect(validateUploadFile({ name: "a.CSV", size: 10 }, { extensions: [".csv"] })).toBeNull();
    expect(validateUploadFile({ name: "a.xlsx", size: 10 }, { extensions: [".csv"] })).toMatch(/Only .csv/);
    expect(validateUploadFile({ name: "a.csv", size: 0 }, { extensions: [".csv"] })).toMatch(/empty/);
    expect(validateUploadFile({ name: "a.pdf", size: 10 * 1024 * 1024 + 1 }, { extensions: [".pdf"] })).toMatch(/10.0 MB/);
  });

  it("only follows absolute http(s) download URLs", () => {
    expect(safeDownloadUrl("https://minio.local/exports/x.csv?X-Amz-Signature=1")).toMatch(/^https:/);
    expect(safeDownloadUrl("javascript:alert(1)")).toBeNull();
    expect(safeDownloadUrl("data:text/csv,a")).toBeNull();
    expect(safeDownloadUrl("/relative/path")).toBeNull();
    expect(safeDownloadUrl("memory://imports/key")).toBeNull();
    expect(safeDownloadUrl(42)).toBeNull();
  });

  it("recognises optimistic-lock conflicts only", () => {
    expect(isVersionConflict({ status: 409, problem: { detail: "Contact was changed by someone else; reload and retry" } })).toBe(true);
    expect(isVersionConflict({ status: 409, problem: { detail: "A tag with this name already exists" } })).toBe(false);
    expect(isVersionConflict({ status: 422, problem: { detail: "changed by someone else" } })).toBe(false);
    expect(isVersionConflict(null)).toBe(false);
  });

  it("backs off polling and eventually stops", () => {
    expect(nextPollDelay(0)).toBe(1500);
    expect(nextPollDelay(5)! > nextPollDelay(1)!).toBe(true);
    expect(nextPollDelay(40)).toBe(15000);
    expect(nextPollDelay(60)).toBeNull();
    expect(formatBytes(2048)).toBe("2.0 KB");
  });
});

describe("CSV import preview", () => {
  it("classifies columns and flags problems like the service", () => {
    const p = previewCsv("email,First_Name,custom.plan,custom.nope,phone\na@x.test,Ana,pro,1,\n", new Set(["plan"]));
    expect(p.columns.map((c) => c.status)).toEqual(["standard", "standard", "custom", "unknown", "standard"]);
    expect(p.problems.join(" ")).toMatch(/Unknown columns: custom.nope/);
    expect(p.rows).toEqual([["a@x.test", "Ana", "pro", "1", ""]]);
  });

  it("requires an email or phone column and unique names", () => {
    const p = previewCsv("first_name,first_name\nA,B\n", new Set());
    expect(p.problems).toContain("The file needs an email or a phone column.");
    expect(p.problems).toContain("Column names must be unique.");
    expect(previewCsv("", new Set()).problems).toEqual(["The file has no header row."]);
  });

  it("builds import options with optional consent evidence", () => {
    const base = { region: "us", source: "fair", consentChannels: [] as ("phone" | "email")[], consentStatus: "granted" as const, lawfulBasis: "consent", consentSource: "", policyVersion: "", evidenceRef: "" };
    expect(JSON.parse(buildImportOptions(base).options!)).toEqual({ default_region: "US", source: "fair" });
    expect(buildImportOptions({ ...base, region: "USA" }).errors.region).toBeTruthy();
    expect(buildImportOptions({ ...base, consentChannels: ["phone"] }).errors.consentSource).toBeTruthy();
    const withConsent = JSON.parse(buildImportOptions({ ...base, consentChannels: ["phone"], consentSource: "web_form", policyVersion: "v2" }).options!);
    expect(withConsent.consent).toEqual({ channels: ["phone"], status: "granted", lawful_basis: "consent", source: "web_form", policy_version: "v2", evidence_ref: null });
  });
});

const contact: Contact = {
  id: "c1", first_name: "Ana", last_name: null, company: null, email: "ana@x.test", phone_e164: "+15551230000", status: "active",
  country: "US", locale: null, timezone: null, source: null, owner_id: null, organization_id: null,
  custom_fields: { plan: "pro", seats: 3 }, tags: [], version: 7, created_at: "", updated_at: "",
};

describe("contact form", () => {
  const defs = [field({ key: "plan", type: "select", options: ["basic", "pro"], required: true }), field({ key: "seats", type: "number" })];

  it("validates core fields and needs some identity", () => {
    const r = buildContact({ ...emptyContactForm(), email: "bad", country: "usa", locale: "EN" }, []);
    expect(r.errors).toMatchObject({ email: expect.any(String), country: expect.any(String), locale: expect.any(String) });
    expect(buildContact(emptyContactForm(), []).errors._form).toMatch(/needs a name/);
  });

  it("enforces required custom fields on create only", () => {
    const form = { ...emptyContactForm(), first_name: "Bo" };
    expect(buildContact(form, defs, "create").errors["custom_fields.plan"]).toBe("Required");
    expect(buildContact(form, defs, "edit").errors["custom_fields.plan"]).toBeUndefined();
    expect(coerceCustom(field({ type: "boolean" }), "maybe").error).toBeTruthy();
    expect(coerceCustom(field({ type: "date" }), "2026-02-30x").error).toBeTruthy();
  });

  it("patches only changed fields, clears custom values with null, and sends the version", () => {
    const form = contactToForm(contact);
    expect(contactPatch(contact, buildContact(form, defs, "edit"))).toEqual({});
    const edited = { ...form, first_name: "Anna", custom: { ...form.custom, seats: "" } };
    expect(contactPatch(contact, buildContact(edited, defs, "edit"))).toEqual({ first_name: "Anna", custom_fields: { seats: null }, expected_version: 7 });
  });

  it("names contacts sensibly", () => {
    expect(contactName(contact)).toBe("Ana");
    expect(contactName({ ...contact, first_name: null, company: "Acme" })).toBe("Acme");
    expect(contactName(null)).toBe("Unknown contact");
  });
});

describe("activity timeline", () => {
  it("merges records newest first", () => {
    const items = buildActivity({
      notes: [{ id: "n", contact_id: "c", text: "hi", source_type: "call", source_id: null, created_by: "u", created_at: "2026-01-03T00:00:00Z" }],
      consents: [{ id: "k", contact_id: "c", channel: "phone", status: "withdrawn", lawful_basis: "consent", source: "call", source_ref: null, policy_version: null, evidence_ref: null, captured_at: "2026-01-02T00:00:00Z", recorded_by: "u", created_at: "" }],
      leads: [{ id: "l", contact_id: "c", status: "booked", source: "web", score: null, qualified: null, owner_id: null, campaign_id: null, call_id: null, booking_id: null, last_outcome: "booked", version: 1, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-04T00:00:00Z" }],
    });
    expect(items.map((i) => i.title)).toEqual(["Lead booked", "Note from call", "Phone consent withdrawn", "Lead created"]);
    expect(reasonLabel("no_consent")).toBe("Consent withdrawn");
    expect(reasonLabel("brand_new")).toBe("brand new");
  });
});

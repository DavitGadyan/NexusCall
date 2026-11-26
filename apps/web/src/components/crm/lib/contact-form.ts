/** Contact create/edit form model: validation that mirrors ContactIn/ContactPatch, custom-field coercion, patch diff. */
import type { Contact, FieldDef } from "./types";

export type ContactForm = {
  first_name: string;
  last_name: string;
  company: string;
  email: string;
  phone: string;
  country: string;
  locale: string;
  timezone: string;
  source: string;
  status: "active" | "inactive";
  custom: Record<string, string>;
};

export function emptyContactForm(): ContactForm {
  return { first_name: "", last_name: "", company: "", email: "", phone: "", country: "", locale: "", timezone: "", source: "", status: "active", custom: {} };
}

function text(v: unknown): string {
  if (v === null || v === undefined) return "";
  return typeof v === "string" ? v : String(v);
}

export function contactToForm(c: Contact): ContactForm {
  const custom: Record<string, string> = {};
  for (const [k, v] of Object.entries(c.custom_fields ?? {})) custom[k] = text(v);
  return {
    first_name: text(c.first_name),
    last_name: text(c.last_name),
    company: text(c.company),
    email: text(c.email),
    phone: text(c.phone_e164),
    country: text(c.country),
    locale: text(c.locale),
    timezone: text(c.timezone),
    source: text(c.source),
    status: c.status === "inactive" ? "inactive" : "active",
    custom,
  };
}

const LIMITS: Partial<Record<keyof ContactForm, number>> = { first_name: 100, last_name: 100, company: 200, email: 320, phone: 32, timezone: 64, source: 64 };

/** Coerces one custom-field value by its definition type; `undefined` means "not set". */
export function coerceCustom(def: FieldDef, raw: string, opts: { enforceRequired?: boolean } = {}): { value?: unknown; error?: string } {
  const v = raw.trim();
  if (!v) return def.required && opts.enforceRequired ? { error: "Required" } : {};
  switch (def.type) {
    case "number": {
      const n = Number(v);
      return Number.isFinite(n) ? { value: n } : { error: "Enter a number" };
    }
    case "boolean":
      return v === "true" || v === "false" ? { value: v === "true" } : { error: "Choose yes or no" };
    case "date":
      return /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(v)) ? { value: v } : { error: "Use YYYY-MM-DD" };
    case "select":
      return def.options.includes(v) ? { value: v } : { error: "Choose one of the options" };
    default:
      return v.length > 2000 ? { error: "At most 2000 characters" } : { value: v };
  }
}

export type BuiltContact = {
  body: Record<string, unknown>;
  /** every defined custom field, `null` when empty (the service merges and treats null as "remove") */
  customAll: Record<string, unknown>;
  errors: Record<string, string>;
};

/**
 * Validates the form and returns a ContactIn body (nulls for cleared optional fields). Required custom fields
 * are enforced on create only, like the service.
 */
export function buildContact(form: ContactForm, defs: readonly FieldDef[], mode: "create" | "edit" = "create"): BuiltContact {
  const errors: Record<string, string> = {};
  const body: Record<string, unknown> = {};
  for (const key of ["first_name", "last_name", "company", "email", "phone", "timezone", "source"] as const) {
    const v = form[key].trim();
    const max = LIMITS[key] ?? 200;
    if (v.length > max) errors[key] = `At most ${max} characters`;
    body[key] = v || null;
  }
  const email = form.email.trim();
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) errors.email = "Enter a valid email address";
  const phone = form.phone.trim();
  if (phone && !/^\+?[0-9 ().-]{4,31}$/.test(phone)) errors.phone = "Enter a valid phone number";
  const country = form.country.trim().toUpperCase();
  if (country && !/^[A-Z]{2}$/.test(country)) errors.country = "Two-letter country code, e.g. US";
  body.country = country || null;
  const locale = form.locale.trim();
  if (locale && !/^[a-z]{2}(-[A-Z]{2})?$/.test(locale)) errors.locale = "Like en or en-US";
  body.locale = locale || null;
  body.status = form.status;
  if (!body.first_name && !body.last_name && !body.company && !body.email && !body.phone) {
    errors._form = "A contact needs a name, a company, an email or a phone number.";
  }
  const custom: Record<string, unknown> = {};
  const customAll: Record<string, unknown> = {};
  for (const def of defs) {
    const r = coerceCustom(def, form.custom[def.key] ?? "", { enforceRequired: mode === "create" });
    if (r.error) errors[`custom_fields.${def.key}`] = r.error;
    else if (r.value !== undefined) custom[def.key] = r.value;
    customAll[def.key] = r.value ?? null;
  }
  body.custom_fields = custom;
  return { body, customAll, errors };
}

/** Only the fields that changed, plus `expected_version` (optimistic lock). Custom fields are sent per key. */
export function contactPatch(original: Contact, built: Pick<BuiltContact, "body" | "customAll">): Record<string, unknown> {
  const before: Record<string, unknown> = {
    first_name: original.first_name, last_name: original.last_name, company: original.company, email: original.email,
    phone: original.phone_e164, timezone: original.timezone, source: original.source, country: original.country,
    locale: original.locale, status: original.status,
  };
  const patch: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(built.body)) {
    if (k === "custom_fields") continue;
    if ((before[k] ?? null) !== (v ?? null)) patch[k] = v;
  }
  const changed: Record<string, unknown> = {};
  const current = original.custom_fields ?? {};
  for (const [k, v] of Object.entries(built.customAll)) {
    if ((current[k] ?? null) !== (v ?? null)) changed[k] = v;
  }
  if (Object.keys(changed).length) patch.custom_fields = changed;
  if (Object.keys(patch).length === 0) return {};
  return { ...patch, expected_version: original.version };
}

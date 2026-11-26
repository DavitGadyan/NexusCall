/**
 * Column preview for CRM CSV imports. Mirrors crm_service `imports.check_header`: 1..100 unique columns, only
 * standard columns or `custom.<key>` of defined custom fields, and an email or phone column. The service
 * re-checks the whole file; the preview reads only the first few rows.
 */
import { parseCsv } from "@/lib/csv";

export const STANDARD_COLUMNS = ["first_name", "last_name", "company", "email", "phone", "timezone", "country", "locale", "source"] as const;
export const MAX_COLUMNS = 100;
/** Bytes read from the start of the file for the preview (the whole file is uploaded). */
export const PREVIEW_BYTES = 64 * 1024;

export type ColumnInfo = { name: string; status: "standard" | "custom" | "unknown" | "duplicate" | "empty" };
export type CsvPreview = { columns: ColumnInfo[]; rows: string[][]; problems: string[]; previewRows: number };

export function previewCsv(text: string, customKeys: ReadonlySet<string>, maxRows = 5): CsvPreview {
  const source = text.length >= PREVIEW_BYTES ? text.slice(0, text.lastIndexOf("\n") > 0 ? text.lastIndexOf("\n") : text.length) : text;
  const table = parseCsv(source);
  const problems: string[] = [];
  const header = (table[0] ?? []).map((h) => h.trim().toLowerCase());
  if (header.length === 0) return { columns: [], rows: [], problems: ["The file has no header row."], previewRows: 0 };
  if (header.length > MAX_COLUMNS) problems.push(`The header has ${header.length} columns; at most ${MAX_COLUMNS} are allowed.`);
  const seen = new Set<string>();
  const columns: ColumnInfo[] = header.map((name) => {
    let status: ColumnInfo["status"];
    if (!name) status = "empty";
    else if (seen.has(name)) status = "duplicate";
    else if ((STANDARD_COLUMNS as readonly string[]).includes(name)) status = "standard";
    else if (name.startsWith("custom.") && customKeys.has(name.slice(7))) status = "custom";
    else status = "unknown";
    seen.add(name);
    return { name, status };
  });
  const unknown = columns.filter((c) => c.status === "unknown").map((c) => c.name);
  if (unknown.length) problems.push(`Unknown columns: ${unknown.slice(0, 10).join(", ")}${unknown.length > 10 ? "…" : ""}. Use the standard names or custom.<key>.`);
  if (columns.some((c) => c.status === "duplicate")) problems.push("Column names must be unique.");
  if (columns.some((c) => c.status === "empty")) problems.push("A column has no name.");
  if (!header.includes("email") && !header.includes("phone")) problems.push("The file needs an email or a phone column.");
  const body = table.slice(1);
  return { columns, rows: body.slice(0, maxRows), problems, previewRows: body.length };
}

export type ImportOptions = {
  default_region: string;
  source: string | null;
  consent?: { channels: ("phone" | "email")[]; status: "granted" | "withdrawn"; lawful_basis: string; source: string; policy_version?: string | null; evidence_ref?: string | null };
};

/** Builds the `options` form field (JSON, ≤ 4000 characters) of `POST /api/v1/imports`. */
export function buildImportOptions(input: {
  region: string;
  source: string;
  consentChannels: ("phone" | "email")[];
  consentStatus: "granted" | "withdrawn";
  lawfulBasis: string;
  consentSource: string;
  policyVersion: string;
  evidenceRef: string;
}): { options?: string; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const region = input.region.trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(region)) errors.region = "Two-letter country code, e.g. US";
  const opts: ImportOptions = { default_region: region, source: input.source.trim() || null };
  if (opts.source && opts.source.length > 64) errors.source = "At most 64 characters";
  if (input.consentChannels.length > 0) {
    const src = input.consentSource.trim();
    if (!src) errors.consentSource = "Say where consent was captured";
    else if (src.length > 64) errors.consentSource = "At most 64 characters";
    opts.consent = {
      channels: input.consentChannels,
      status: input.consentStatus,
      lawful_basis: input.lawfulBasis,
      source: src,
      policy_version: input.policyVersion.trim() || null,
      evidence_ref: input.evidenceRef.trim() || null,
    };
  }
  const json = JSON.stringify(opts);
  if (json.length > 4000) errors._form = "Import options are too long";
  return Object.keys(errors).length ? { errors } : { options: json, errors };
}

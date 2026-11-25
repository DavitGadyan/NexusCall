/** CSV helpers for bulk invitations: RFC 4180 parsing, per-row validation, safe report export. */

export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const src = text.replace(/^﻿/, "");
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (quoted) {
      if (c === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += c;
      continue;
    }
    if (c === '"' && field === "") quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && src[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field !== "" || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((f) => f.trim() !== ""));
}

const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,}$/;
export const MAX_BULK_ROWS = 200;

export type InviteRow = { line: number; email: string; roleKeys: string[]; team: string | null; errors: string[] };

/**
 * Columns: `email, roles, team`. Roles are separated by `;` or `|`. A header row is optional.
 * `grantable` is the set of role keys the current admin may grant (server re-checks).
 */
export function parseInviteCsv(
  text: string,
  opts: { knownRoles: ReadonlySet<string>; grantable: ReadonlySet<string>; teams?: ReadonlySet<string> },
): { rows: InviteRow[]; fileErrors: string[] } {
  const table = parseCsv(text);
  const fileErrors: string[] = [];
  let start = 0;
  if (table[0] && (table[0][0] ?? "").trim().toLowerCase() === "email") start = 1;
  const body = table.slice(start);
  if (body.length === 0) fileErrors.push("The file has no rows.");
  if (body.length > MAX_BULK_ROWS) fileErrors.push(`At most ${MAX_BULK_ROWS} rows per upload (found ${body.length}).`);
  const seen = new Set<string>();
  const rows = body.slice(0, MAX_BULK_ROWS).map((cells, i): InviteRow => {
    const email = (cells[0] ?? "").trim().toLowerCase();
    const roleKeys = (cells[1] ?? "")
      .split(/[;|]/)
      .map((r) => r.trim())
      .filter(Boolean);
    const team = (cells[2] ?? "").trim() || null;
    const errors: string[] = [];
    if (!EMAIL.test(email)) errors.push("invalid email");
    else if (seen.has(email)) errors.push("duplicate email in file");
    seen.add(email);
    if (roleKeys.length === 0) errors.push("no role");
    if (roleKeys.length > 10) errors.push("too many roles");
    for (const r of roleKeys) {
      if (!opts.knownRoles.has(r)) errors.push(`unknown role "${r}"`);
      else if (!opts.grantable.has(r)) errors.push(`you cannot grant "${r}"`);
    }
    if (team && opts.teams && !opts.teams.has(team)) errors.push(`unknown team "${team}"`);
    return { line: i + start + 1, email, roleKeys, team, errors };
  });
  return { rows, fileErrors };
}

/** Neutralises spreadsheet formulas (CSV injection) and quotes fields when needed. */
export function csvCell(value: unknown): string {
  let s = value === null || value === undefined ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(rows: readonly (readonly unknown[])[]): string {
  return rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

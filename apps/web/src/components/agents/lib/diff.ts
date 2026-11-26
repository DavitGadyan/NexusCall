/**
 * Line diff for prompt versions (LCS over lines, O(n·m) with a size cap) and a field-level comparison of two
 * versions. Output is plain text segments rendered as text nodes — never HTML.
 */
import type { AgentVersion } from "./types";

export type DiffLine = { kind: "same" | "add" | "del"; text: string; a?: number; b?: number };

/** Above this many line pairs the LCS table is skipped and the texts are shown as replaced wholesale. */
export const MAX_CELLS = 4_000_000;

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  // Trim the common prefix and suffix first: prompts usually change in one place.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const head: DiffLine[] = a.slice(0, start).map((text, i) => ({ kind: "same", text, a: i + 1, b: i + 1 }));
  const tail: DiffLine[] = a.slice(endA).map((text, i) => ({ kind: "same", text, a: endA + i + 1, b: endB + i + 1 }));
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const n = midA.length;
  const m = midB.length;
  const mid: DiffLine[] = [];
  if (n * m > MAX_CELLS) {
    midA.forEach((text, i) => mid.push({ kind: "del", text, a: start + i + 1 }));
    midB.forEach((text, j) => mid.push({ kind: "add", text, b: start + j + 1 }));
    return [...head, ...mid, ...tail];
  }
  // lcs[i][j] = LCS length of midA[i..] and midB[j..]
  const lcs: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    const row = lcs[i]!;
    const next = lcs[i + 1]!;
    for (let j = m - 1; j >= 0; j--) row[j] = midA[i] === midB[j] ? next[j + 1]! + 1 : Math.max(next[j]!, row[j + 1]!);
  }
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (midA[i] === midB[j]) {
      mid.push({ kind: "same", text: midA[i]!, a: start + i + 1, b: start + j + 1 });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      mid.push({ kind: "del", text: midA[i]!, a: start + i + 1 });
      i++;
    } else {
      mid.push({ kind: "add", text: midB[j]!, b: start + j + 1 });
      j++;
    }
  }
  while (i < n) mid.push({ kind: "del", text: midA[i]!, a: start + ++i });
  while (j < m) mid.push({ kind: "add", text: midB[j]!, b: start + ++j });
  return [...head, ...mid, ...tail];
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  return { added: lines.filter((l) => l.kind === "add").length, removed: lines.filter((l) => l.kind === "del").length };
}

/** Collapses long unchanged runs, keeping `context` lines around each change. */
export function withContext(lines: DiffLine[], context = 3): (DiffLine | { kind: "gap"; count: number })[] {
  const keep = new Array(lines.length).fill(false);
  lines.forEach((l, idx) => {
    if (l.kind === "same") return;
    for (let k = Math.max(0, idx - context); k <= Math.min(lines.length - 1, idx + context); k++) keep[k] = true;
  });
  const out: (DiffLine | { kind: "gap"; count: number })[] = [];
  let gap = 0;
  lines.forEach((l, idx) => {
    if (keep[idx]) {
      if (gap) out.push({ kind: "gap", count: gap });
      gap = 0;
      out.push(l);
    } else gap++;
  });
  if (gap) out.push({ kind: "gap", count: gap });
  return out;
}

const TEXT_FIELDS = ["system_prompt", "greeting", "disclosure_text"] as const;
export type TextField = (typeof TEXT_FIELDS)[number];
export const TEXT_FIELD_LABEL: Record<TextField, string> = { system_prompt: "System prompt", greeting: "Greeting", disclosure_text: "AI disclosure" };

const SCALAR_FIELDS: { key: keyof AgentVersion; label: string }[] = [
  { key: "model", label: "Model" },
  { key: "temperature", label: "Temperature" },
  { key: "voice_id", label: "Voice" },
  { key: "max_call_seconds", label: "Max call length (s)" },
  { key: "tool_ids", label: "Tools" },
  { key: "knowledge_source_ids", label: "Knowledge sources" },
  { key: "escalation_rules", label: "Escalation rules" },
];

function show(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) {
    if (v.length === 0) return "none";
    return v.map((x) => (typeof x === "object" && x ? Object.values(x).filter(Boolean).join(" → ") : String(x))).join(", ");
  }
  return String(v);
}

export type VersionComparison = {
  text: { field: TextField; lines: DiffLine[] }[];
  settings: { label: string; before: string; after: string }[];
};

export function compareVersions(a: AgentVersion, b: AgentVersion): VersionComparison {
  const text = TEXT_FIELDS.filter((f) => (a[f] ?? "") !== (b[f] ?? "")).map((field) => ({ field, lines: diffLines(a[field] ?? "", b[field] ?? "") }));
  const norm = (v: unknown) => JSON.stringify(Array.isArray(v) ? [...v].map((x) => JSON.stringify(x)).sort() : v ?? null);
  const settings = SCALAR_FIELDS.filter((f) => norm(a[f.key]) !== norm(b[f.key])).map((f) => ({ label: f.label, before: show(a[f.key]), after: show(b[f.key]) }));
  return { text, settings };
}

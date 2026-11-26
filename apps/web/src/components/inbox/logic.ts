/** Pure rules for the inbox and the AI-draft review (unit-tested). The backend enforces all of them again. */
import type { Attachment, DraftDetail, DraftVersion } from "./types";

export type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export type FlagInfo = { flag: string; label: string; description: string; tone: Tone };

const FLAG_INFO: Record<string, Omit<FlagInfo, "flag">> = {
  ungrounded: { label: "Not grounded", description: "Parts of this reply are not backed by your knowledge sources. Check every factual claim.", tone: "danger" },
  sensitive: { label: "Sensitive", description: "The message touches a sensitive topic (for example health, money, legal or personal data).", tone: "danger" },
  injection_suspected: { label: "Possible manipulation", description: "The incoming email may contain instructions aimed at the AI. Read the original carefully.", tone: "danger" },
  needs_human: { label: "Needs a person", description: "The AI judged that a person should handle this reply.", tone: "warn" },
  output_blocked: { label: "Output blocked", description: "Part of the AI output was blocked by policy.", tone: "warn" },
  low_confidence: { label: "Low confidence", description: "The AI is unsure about this reply.", tone: "warn" },
};

/** Quality flags of a version, most serious first; unknown flags are shown verbatim as warnings. */
export function draftFlags(version: Pick<DraftVersion, "quality"> | null | undefined, extra: { sensitive?: boolean } = {}): FlagInfo[] {
  const raw = Array.isArray(version?.quality?.flags) ? (version!.quality.flags as unknown[]).filter((f): f is string => typeof f === "string") : [];
  const set = new Set(raw);
  if (extra.sensitive) set.add("sensitive");
  const out = [...set].map((flag) => ({ flag, ...(FLAG_INFO[flag] ?? { label: flag.replace(/_/g, " "), description: "Flag raised by the drafting checks.", tone: "warn" as Tone }) }));
  const rank = (t: Tone) => (t === "danger" ? 0 : t === "warn" ? 1 : 2);
  return out.sort((a, b) => rank(a.tone) - rank(b.tone) || a.flag.localeCompare(b.flag));
}

export function confidence(version: Pick<DraftVersion, "quality"> | null | undefined): number | null {
  const c = version?.quality?.confidence;
  return typeof c === "number" && Number.isFinite(c) ? Math.max(0, Math.min(1, c)) : null;
}

export function currentVersion(d: Pick<DraftDetail, "versions" | "current_version">): DraftVersion | null {
  return d.versions.find((v) => v.version === d.current_version) ?? null;
}

/** Approve is possible only for the current version of a draft waiting for review (send needs approval). */
export function approveBlockReason(d: Pick<DraftDetail, "status" | "current_version">, version: number, opts: { unsavedEdits?: boolean } = {}): string | null {
  if (opts.unsavedEdits) return "Save or discard your edits first.";
  if (d.status !== "pending_review") return `A draft that is ${d.status.replace(/_/g, " ")} cannot be approved.`;
  if (d.current_version < 1) return "There is no draft text yet.";
  if (version !== d.current_version) return "Only the latest version can be approved.";
  return null;
}

export const EDITABLE = ["pending_review", "generation_failed", "approved", "send_failed"];

export function canEdit(status: string): boolean {
  return EDITABLE.includes(status);
}

export function canReject(status: string): boolean {
  return EDITABLE.includes(status) || status === "queued";
}

export function canEscalate(status: string): boolean {
  return status !== "sent" && status !== "rejected";
}

/** Mirrors email_inbox_service.mime.reply_subject. */
export function replySubject(subject: string): string {
  const s = subject.trim();
  return /^re:/i.test(s) ? s : `Re: ${s}`.trim();
}

export type AttachmentState = { kind: "download" | "blocked" | "unavailable"; label: string; tone: Tone };

/** Only scanned-clean files are offered for download; an infected or quarantined file never is. */
export function attachmentState(a: Pick<Attachment, "downloadable" | "quarantined" | "scan_status" | "rejection_reason">): AttachmentState {
  if (a.quarantined || a.scan_status === "infected") return { kind: "blocked", label: "Malware found — blocked", tone: "danger" };
  if (a.scan_status === "rejected") return { kind: "blocked", label: a.rejection_reason ? `Rejected: ${a.rejection_reason.replace(/_/g, " ")}` : "Rejected", tone: "danger" };
  if (a.scan_status === "purged") return { kind: "unavailable", label: "Removed (retention)", tone: "neutral" };
  if (a.scan_status === "error") return { kind: "unavailable", label: "Scan failed — not available", tone: "warn" };
  if (!a.downloadable) return { kind: "unavailable", label: "Not scanned — not available", tone: "warn" };
  return { kind: "download", label: a.scan_status === "clean" ? "Scanned clean" : "Not scanned (development only)", tone: a.scan_status === "clean" ? "ok" : "warn" };
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function draftStatusTone(status: string): Tone {
  switch (status) {
    case "pending_review":
      return "warn";
    case "approved":
    case "sending":
      return "brand";
    case "sent":
      return "ok";
    case "send_failed":
    case "generation_failed":
      return "danger";
    default:
      return "neutral";
  }
}

export function priorityTone(p: string): Tone {
  return p === "urgent" ? "danger" : p === "high" ? "warn" : "neutral";
}

const ACTION_LABELS: Record<string, string> = {
  generated: "AI wrote a draft",
  edited: "Edited (new version)",
  regenerated: "Asked the AI for a new draft",
  rejected: "Rejected",
  assigned: "Assigned",
  escalated: "Escalated",
  approved: "Approved for sending",
  send_canceled: "Pending send canceled",
  send_retry: "Send retried",
  sent: "Sent",
  send_failed: "Send failed",
  generation_failed: "Drafting failed",
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action.replace(/_/g, " ");
}

export function assigneeLabel(assignee: string | null, me: string | null): string {
  if (!assignee) return "Unassigned";
  if (me && assignee === me) return "You";
  return `${assignee.slice(0, 8)}…`;
}

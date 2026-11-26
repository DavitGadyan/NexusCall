/** Pure helpers shared by the call, booking, inbox and approval pages (unit-tested in shared.test.ts). */
import type { Call, Contact, VoiceSession } from "./types";

export const CALL_STATES = [
  "queued",
  "scheduled",
  "initiating",
  "ringing",
  "connected",
  "in_progress",
  "completed",
  "failed",
  "canceled",
  "opted_out",
] as const;

/** Mirrors call_service.state_machine.LIVE. */
export const LIVE_STATES = ["initiating", "ringing", "connected", "in_progress"] as const;

export function isLive(state: string): boolean {
  return (LIVE_STATES as readonly string[]).includes(state);
}

export type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export function callStateTone(state: string): Tone {
  switch (state) {
    case "connected":
    case "in_progress":
      return "ok";
    case "initiating":
    case "ringing":
      return "brand";
    case "queued":
    case "scheduled":
      return "warn";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

/** 65 -> "1:05", 3723 -> "1:02:03". Negative or missing values render as an em dash. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** Seconds a live call has been in its current phase: talking time once answered, else since dialing began. */
export function liveElapsedSeconds(call: Pick<Call, "answered_at" | "started_at" | "initiated_at" | "created_at">, nowMs: number): number {
  const ref = call.answered_at ?? call.started_at ?? call.initiated_at ?? call.created_at;
  const t = new Date(ref).getTime();
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.floor((nowMs - t) / 1000));
}

export function formatCost(micros: number | null | undefined): string {
  if (!micros) return "—";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(micros / 1_000_000);
}

export type TimelineEntry = { at: string; label: string; detail?: string; tone?: Tone };

/** Call timeline from the lifecycle timestamps and the voice session's handoffs, oldest first. */
export function buildTimeline(call: Call, session?: VoiceSession | null): TimelineEntry[] {
  const out: TimelineEntry[] = [];
  const push = (at: string | null | undefined, label: string, detail?: string, tone?: Tone) => {
    if (at && !Number.isNaN(new Date(at).getTime())) out.push({ at, label, detail, tone });
  };
  push(call.created_at, call.is_test ? "Test call created" : "Call created", call.direction === "inbound" ? "Inbound" : "Outbound");
  push(call.scheduled_for, "Scheduled for");
  push(call.initiated_at, "Dialing started", call.attempt_count > 1 ? `Attempt ${call.attempt_count}` : undefined);
  push(call.started_at, "Provider call started");
  push(call.answered_at, "Answered", undefined, "ok");
  if (session) {
    push(session.started_at, "AI voice session started");
    for (const h of session.handoffs) push(h.created_at, "Handoff to a person", `${h.trigger.replace(/_/g, " ")} — ${h.status.replace(/_/g, " ")}`, "warn");
    if (session.ended_at && session.ended_at !== call.ended_at) push(session.ended_at, "AI voice session ended", session.end_reason ?? undefined);
  }
  const endTone: Tone = call.state === "failed" ? "danger" : call.state === "opted_out" ? "warn" : "neutral";
  push(call.ended_at, `Ended (${call.state.replace(/_/g, " ")})`, call.end_reason?.replace(/_/g, " "), endTone);
  return out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

/**
 * Accepts only absolute http(s) URLs (presigned object-store links). Anything else — `javascript:`, `data:`,
 * relative or malformed values — is refused, so a compromised or buggy upstream cannot inject a script URL.
 */
export function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 8192) return null;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

/** Seconds until a short-lived URL fetched at `fetchedAtMs` expires (never negative). */
export function secondsLeft(fetchedAtMs: number, expiresInSeconds: number, nowMs: number): number {
  return Math.max(0, Math.ceil(expiresInSeconds - (nowMs - fetchedAtMs) / 1000));
}

export function contactLabel(c: Pick<Contact, "first_name" | "last_name" | "email" | "id"> | null | undefined): string {
  if (!c) return "—";
  const name = [c.first_name, c.last_name].filter(Boolean).join(" ").trim();
  return name || c.email || `Contact ${c.id.slice(0, 8)}`;
}

/** `ms` offset in a recording as m:ss. */
export function formatOffset(ms: number): string {
  return formatDuration(Math.floor(ms / 1000));
}

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

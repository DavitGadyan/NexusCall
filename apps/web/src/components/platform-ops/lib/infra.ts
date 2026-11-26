/**
 * Platform infrastructure (tenant_service `/api/v1/platform/infra/*`): service health, broker queues and a
 * metadata-only dead-letter view. Message bodies are never returned; replay and discard need
 * `platform:queues:replay`, a recent step-up and a reason, and are audited.
 */

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export type ServiceHealth = { service: string; status: "ready" | "not_ready" | "unreachable"; latency_ms?: number | null; checks?: Record<string, boolean> };

export type QueueInfo = {
  name: string;
  kind: "task" | "event" | "dead_letter" | "other";
  type?: string | null;
  messages?: number;
  ready?: number;
  unacked?: number;
  consumers?: number;
  publish_rate?: number;
  deliver_rate?: number;
};

export type QueuesOut = { dead_letter_total: number; items: QueueInfo[] };

export type DeadLetter = {
  queue: string;
  kind: "task" | "event" | "unknown";
  name?: string | null;
  tenant_id?: string | null;
  correlation_id?: string | null;
  death_count?: number | null;
  death_reason?: string | null;
  first_death_queue?: string | null;
  size_bytes?: number;
  identifiers?: Record<string, string>;
};

export type DlqActionOut = { action: "replayed" | "discarded"; processed: number; queue: string; target?: string | null };

export const DLQ_NAME = /^[A-Za-z0-9._-]{1,200}\.dlq$/;

export function serviceTone(status: string): Tone {
  if (status === "ready") return "ok";
  if (status === "not_ready") return "warn";
  return "danger";
}

export function summarizeServices(items: ServiceHealth[]): { ready: number; notReady: number; unreachable: number } {
  return {
    ready: items.filter((s) => s.status === "ready").length,
    notReady: items.filter((s) => s.status === "not_ready").length,
    unreachable: items.filter((s) => s.status === "unreachable").length,
  };
}

/** Unhealthy first, then by name, so a problem is at the top of the list. */
export function sortServices(items: ServiceHealth[]): ServiceHealth[] {
  const rank = { unreachable: 0, not_ready: 1, ready: 2 } as Record<string, number>;
  return [...items].sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || a.service.localeCompare(b.service));
}

export function failingChecks(s: ServiceHealth): string[] {
  return Object.entries(s.checks ?? {})
    .filter(([, ok]) => !ok)
    .map(([k]) => k);
}

export function groupQueues(items: QueueInfo[]): Record<QueueInfo["kind"], QueueInfo[]> {
  const out: Record<QueueInfo["kind"], QueueInfo[]> = { task: [], event: [], dead_letter: [], other: [] };
  for (const q of items) (out[q.kind] ?? out.other).push(q);
  for (const k of Object.keys(out) as QueueInfo["kind"][]) out[k].sort((a, b) => (b.messages ?? 0) - (a.messages ?? 0) || a.name.localeCompare(b.name));
  return out;
}

/** A queue with messages but no consumers is stuck; a backlog with consumers is only busy. */
export function queueTone(q: QueueInfo): Tone {
  const n = q.messages ?? 0;
  if (q.kind === "dead_letter") return n > 0 ? "danger" : "ok";
  if (n > 0 && (q.consumers ?? 0) === 0) return "danger";
  if (n > 1000) return "warn";
  return "ok";
}

export function queueNote(q: QueueInfo): string | null {
  const n = q.messages ?? 0;
  if (q.kind === "dead_letter") return n > 0 ? `${n.toLocaleString()} dead letter${n === 1 ? "" : "s"}` : null;
  if (n > 0 && (q.consumers ?? 0) === 0) return "No consumers";
  if (n > 1000) return "Backlog";
  return null;
}

export function validateDlqAction(v: { queue: string; count: string; reason: string }): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!DLQ_NAME.test(v.queue)) errors.queue = "Not a dead-letter queue.";
  const n = /^\d{1,3}$/.test(v.count.trim()) ? Number(v.count.trim()) : NaN;
  if (!Number.isInteger(n) || n < 1 || n > 50) errors.count = "Between 1 and 50 messages per action.";
  const r = v.reason.trim();
  if (r.length < 5) errors.reason = "At least 5 characters.";
  else if (r.length > 300) errors.reason = "At most 300 characters.";
  return errors;
}

export function formatRate(r: number | undefined): string {
  if (!r) return "0/s";
  return `${r < 10 ? r.toFixed(1) : Math.round(r)}/s`;
}

/**
 * Parsing for the notification-service SSE stream (`GET /api/v1/notifications/stream`).
 * `data:` lines carry `{type:"notification", id}` or `{type:"ui", event, data}` with ids only.
 */
export type LiveEvent =
  | { type: "notification"; id: string }
  | { type: "ui"; event: string; data: Record<string, string> };

const EVENT_NAME = /^[a-z][a-z0-9_.:-]{0,63}$/;

export function parseLiveEvent(raw: string): LiveEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.type === "notification" && typeof v.id === "string" && v.id.length <= 64) return { type: "notification", id: v.id };
  if (v.type === "ui" && typeof v.event === "string" && EVENT_NAME.test(v.event)) {
    const data: Record<string, string> = {};
    if (v.data && typeof v.data === "object") {
      for (const [k, d] of Object.entries(v.data as Record<string, unknown>).slice(0, 8)) {
        if (typeof d === "string" && d.length <= 64) data[k] = d;
      }
    }
    return { type: "ui", event: v.event, data };
  }
  return null;
}

/** Exponential backoff with a cap, used when the stream closes (e.g. after a deploy). */
export function reconnectDelay(attempt: number, baseMs = 1000, maxMs = 60_000): number {
  return Math.min(maxMs, baseMs * 2 ** Math.max(0, Math.min(attempt, 16)));
}

export function matchesEvent(evt: LiveEvent, events: readonly string[] | undefined): boolean {
  if (!events || events.length === 0) return true;
  if (evt.type === "notification") return events.includes("notification");
  return events.some((e) => e === evt.event || (e.endsWith(".*") && evt.event.startsWith(e.slice(0, -1))));
}

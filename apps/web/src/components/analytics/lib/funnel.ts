/**
 * Funnels are ordered steps of one measure, so they are drawn as ranked bars in one hue (sequential job,
 * dataviz skill): length encodes the count, the label carries identity, and each step shows its share of
 * the first step. No step is coloured by status.
 */
import type { BookingsOut, CallsOut, DraftsOut, EmailsOut } from "./types";

export type FunnelStep = { key: string; label: string; value: number; share: number | null };

export function funnel(steps: { key: string; label: string; value: number }[]): FunnelStep[] {
  const first = steps[0]?.value ?? 0;
  return steps.map((s) => ({ ...s, value: Math.max(0, s.value), share: first > 0 ? Math.max(0, s.value) / first : null }));
}

export function callFunnel(c: CallsOut): FunnelStep[] {
  return funnel([
    { key: "attempted", label: "Attempted", value: c.attempted },
    { key: "answered", label: "Answered", value: c.answered },
    { key: "completed", label: "Completed", value: c.completed },
    { key: "qualified", label: "Qualified", value: c.qualified },
  ]);
}

export function emailFunnel(e: Pick<EmailsOut, "accepted" | "delivered">): FunnelStep[] {
  return funnel([
    { key: "accepted", label: "Accepted", value: e.accepted },
    { key: "delivered", label: "Delivered", value: e.delivered },
  ]);
}

export function draftFunnel(d: DraftsOut): FunnelStep[] {
  return funnel([
    { key: "requested", label: "Requested", value: d.requested },
    { key: "generated", label: "Generated", value: d.generated },
    { key: "approved", label: "Approved", value: d.approved },
    { key: "sent", label: "Sent", value: d.sent },
  ]);
}

export function bookingFunnel(b: BookingsOut): FunnelStep[] {
  return funnel([
    { key: "created", label: "Created", value: b.created },
    { key: "confirmed", label: "Confirmed", value: b.confirmed },
  ]);
}

/** Cost parts, largest first (ranked bars, one hue). Zero parts are kept so the reader sees them. */
export function costParts(c: { ai_cost_micros: number; call_cost_micros: number; email_cost_micros: number }): { key: string; label: string; value: number }[] {
  return [
    { key: "calls", label: "Calls", value: c.call_cost_micros },
    { key: "ai", label: "AI", value: c.ai_cost_micros },
    { key: "email", label: "Email", value: c.email_cost_micros },
  ].sort((a, b) => b.value - a.value);
}

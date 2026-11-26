/** Pure email-module rules: DNS status, sendability, campaign actions and the delivery report (unit-tested). */
import type { Campaign, Domain, Report, Sender } from "./types";

export type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export function dnsTone(status: string): Tone {
  if (status === "pass" || status === "verified") return "ok";
  if (status === "fail" || status === "failed") return "danger";
  if (status === "disabled") return "neutral";
  return "warn";
}

export function dnsLabel(status: string): string {
  if (status === "pass") return "Passing";
  if (status === "fail") return "Failing";
  if (status === "pending") return "Not checked yet";
  return status.replace(/_/g, " ");
}

/** Only a verified, enabled domain can send (email-service checks again at launch). */
export function senderBlockReason(sender: Pick<Sender, "status" | "domain_status">): string | null {
  if (sender.status !== "active") return "Sender is disabled";
  if (sender.domain_status !== "verified") return "Domain not verified";
  return null;
}

export function domainCanSend(domain: Pick<Domain, "status">): boolean {
  return domain.status === "verified";
}

/** Which lifecycle actions the campaign state allows (mirrors email_service.state_machine). */
export function campaignActions(state: string): { edit: boolean; schedule: boolean; pause: boolean; resume: boolean; cancel: boolean } {
  return {
    edit: state === "draft",
    schedule: state === "draft",
    pause: state === "scheduled" || state === "running",
    resume: state === "paused",
    cancel: state === "draft" || state === "scheduled" || state === "running" || state === "paused",
  };
}

export function campaignTone(state: string): Tone {
  switch (state) {
    case "running":
      return "ok";
    case "scheduled":
      return "brand";
    case "paused":
      return "warn";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

/**
 * Delivery states in pipeline order, with what each one actually means. Provider outcomes only:
 * opens and clicks are not tracked.
 */
export const DELIVERY_STATES: { state: string; label: string; meaning: string; group: "in_flight" | "outcome" | "problem" }[] = [
  { state: "pending", label: "Pending", meaning: "Waiting for the send window", group: "in_flight" },
  { state: "queued", label: "Queued", meaning: "Ready to send", group: "in_flight" },
  { state: "sending", label: "Sending", meaning: "Handing to the provider", group: "in_flight" },
  { state: "accepted", label: "Accepted", meaning: "Provider took the message; no delivery report yet", group: "outcome" },
  { state: "delivered", label: "Delivered", meaning: "Recipient server accepted it (not the same as read)", group: "outcome" },
  { state: "bounced", label: "Bounced", meaning: "Rejected by the recipient server", group: "problem" },
  { state: "complained", label: "Complained", meaning: "Recipient marked it as spam", group: "problem" },
  { state: "rejected", label: "Rejected", meaning: "Provider refused to send it", group: "problem" },
  { state: "unsubscribed", label: "Unsubscribed", meaning: "Recipient unsubscribed after receiving it", group: "problem" },
  { state: "failed", label: "Failed", meaning: "Could not be sent", group: "problem" },
];

export type ReportRow = { state: string; label: string; meaning: string; count: number; share: number; group: string };

/** Rows for every known state (zeros included) plus any unknown states the server reports. */
export function reportRows(report: Pick<Report, "counts" | "recipients">): ReportRow[] {
  const total = report.recipients || Object.values(report.counts).reduce((a, b) => a + b, 0);
  const known = new Set(DELIVERY_STATES.map((d) => d.state));
  const rows: ReportRow[] = DELIVERY_STATES.map((d) => ({ ...d, count: report.counts[d.state] ?? 0, share: total ? (report.counts[d.state] ?? 0) / total : 0 }));
  for (const [state, count] of Object.entries(report.counts)) {
    if (!known.has(state)) rows.push({ state, label: state.replace(/_/g, " "), meaning: "", count, share: total ? count / total : 0, group: "problem" });
  }
  return rows;
}

export type BarSegment = { key: "delivered" | "accepted" | "problem"; label: string; count: number; share: number };

/**
 * The delivery bar folds the states into three outcome groups, ordered so red and green never touch
 * (validated for colour-vision deficiency). Messages still in flight are the unfilled remainder.
 */
export function reportSegments(rows: readonly ReportRow[]): { segments: BarSegment[]; inFlight: number; total: number } {
  const total = rows.reduce((a, r) => a + r.count, 0);
  const sum = (pred: (r: ReportRow) => boolean) => rows.filter(pred).reduce((a, r) => a + r.count, 0);
  const delivered = sum((r) => r.state === "delivered");
  const accepted = sum((r) => r.state === "accepted");
  const problem = sum((r) => r.group === "problem");
  const share = (n: number) => (total ? n / total : 0);
  return {
    total,
    inFlight: sum((r) => r.group === "in_flight"),
    segments: [
      { key: "delivered", label: "Delivered", count: delivered, share: share(delivered) },
      { key: "accepted", label: "Accepted, awaiting report", count: accepted, share: share(accepted) },
      { key: "problem", label: "Bounced, complained, rejected, unsubscribed or failed", count: problem, share: share(problem) },
    ],
  };
}

/** Rate keys from the report, in display order. Anything about opens or clicks is dropped on purpose. */
export function reportRates(rates: Record<string, number>): { key: string; label: string; value: number }[] {
  const labels: Record<string, string> = {
    delivery_rate: "Delivered",
    bounce_rate: "Bounced",
    complaint_rate: "Complaints",
    unsubscribe_rate: "Unsubscribed",
  };
  return Object.entries(rates)
    .filter(([k]) => !/open|click/i.test(k))
    .map(([key, value]) => ({ key, label: labels[key] ?? key.replace(/_/g, " "), value: Number.isFinite(value) ? value : 0 }));
}

export function percent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

/** The server requires an explicit offset; datetime-local is converted to an ISO instant in UTC. */
export function scheduleBody(local: string): { scheduled_at: string } | Record<string, never> | null {
  if (!local) return {};
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return null;
  return { scheduled_at: d.toISOString() };
}

export function isCampaignLive(c: Pick<Campaign, "state">): boolean {
  return c.state === "scheduled" || c.state === "running" || c.state === "paused";
}

/** Template 422s carry `problems: string[]` next to the detail. */
export function templateProblems(problem: Record<string, unknown> | undefined): string[] {
  const p = problem?.problems;
  return Array.isArray(p) ? p.filter((x): x is string => typeof x === "string").slice(0, 20) : [];
}

export const TEMPLATE_VARIABLES = ["first_name", "last_name", "full_name", "sender_name"] as const;

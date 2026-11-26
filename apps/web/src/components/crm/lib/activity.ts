/**
 * Contact activity timeline, assembled from the records crm_service already exposes (there is no activity
 * endpoint): lead changes, consent records, notes (manual and system ones such as call outcomes) and
 * suppressions. Newest first.
 */
import type { Consent, Lead, Note, Suppression } from "./types";

export type ActivityItem = { id: string; at: string; kind: "lead" | "consent" | "note" | "suppression"; title: string; detail?: string };

export function buildActivity(input: { leads?: Lead[]; consents?: Consent[]; notes?: Note[]; suppressions?: Suppression[] }): ActivityItem[] {
  const out: ActivityItem[] = [];
  for (const l of input.leads ?? []) {
    out.push({ id: `lead-created-${l.id}`, at: l.created_at, kind: "lead", title: "Lead created", detail: l.source ? `Source: ${l.source}` : undefined });
    if (l.updated_at !== l.created_at) {
      out.push({
        id: `lead-updated-${l.id}`,
        at: l.updated_at,
        kind: "lead",
        title: `Lead ${l.status.replace(/_/g, " ")}`,
        detail: l.last_outcome ? `Last outcome: ${l.last_outcome.replace(/_/g, " ")}` : undefined,
      });
    }
  }
  for (const c of input.consents ?? []) {
    out.push({
      id: `consent-${c.id}`,
      at: c.captured_at,
      kind: "consent",
      title: `${c.channel === "phone" ? "Phone" : "Email"} consent ${c.status}`,
      detail: `${c.lawful_basis.replace(/_/g, " ")} · ${c.source}`,
    });
  }
  for (const n of input.notes ?? []) {
    out.push({ id: `note-${n.id}`, at: n.created_at, kind: "note", title: n.source_type === "user" ? "Note added" : `Note from ${n.source_type.replace(/_/g, " ")}`, detail: n.text });
  }
  for (const s of input.suppressions ?? []) {
    out.push({ id: `supp-${s.id}`, at: s.created_at, kind: "suppression", title: `Suppressed (${s.channel})`, detail: s.reason.replace(/_/g, " ") });
  }
  return out.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : a.id.localeCompare(b.id)));
}

/** Reason codes of `GET /contacts/{id}/contactability` (crm_service contactability.py). */
export const REASON_LABEL: Record<string, string> = {
  suppressed: "Suppressed",
  no_consent: "Consent withdrawn",
  no_address: "No address on file",
  contact_inactive: "Contact is not active",
};

export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? code.replace(/_/g, " ");
}

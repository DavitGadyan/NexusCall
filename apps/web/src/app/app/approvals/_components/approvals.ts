/** Tool-approval shapes (contracts/openapi/tool_execution.json) and pure display helpers (unit-tested). */
export type Approval = {
  id: string;
  agent_id: string;
  tool_id: string;
  tool_version: number;
  risk_class: number;
  arguments: Record<string, unknown>;
  context: Record<string, unknown>;
  requested_by: { id: string; type: string };
  status: "pending" | "approved" | "rejected" | "expired" | "consumed" | string;
  expires_at: string;
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  consumed_at: string | null;
  result_status: string | null;
  result_reason: string | null;
};

export type DecisionOut = { approval: Approval; execution?: { status: string; reason?: string | null; result?: unknown } | null };

export const APPROVAL_STATUSES = ["pending", "approved", "rejected", "expired", "consumed"] as const;

export function riskLabel(risk: number): { label: string; tone: "neutral" | "warn" | "danger" } {
  if (risk >= 3) return { label: `High risk (class ${risk})`, tone: "danger" };
  if (risk === 2) return { label: `Customer-facing (class ${risk})`, tone: "warn" };
  return { label: `Low risk (class ${risk})`, tone: "neutral" };
}

/** "crm.update_contact" -> "Crm › Update contact". */
export function toolLabel(toolId: string): string {
  return toolId
    .split(/[.:/]/)
    .filter(Boolean)
    .map((p) => p.replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase()))
    .join(" › ");
}

/** Argument values as display text. Objects are pretty-printed JSON; long values are cut, never rendered as HTML. */
export function argText(value: unknown, max = 2000): string {
  let s: string;
  if (value === null || value === undefined) s = "—";
  else if (typeof value === "string") s = value;
  else if (typeof value === "number" || typeof value === "boolean") s = String(value);
  else {
    try {
      s = JSON.stringify(value, null, 2) ?? "—";
    } catch {
      s = "[unrenderable]";
    }
  }
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export type ContextLink = { label: string; href: string | null; value: string };

const CONTEXT_LINKS: Record<string, { label: string; href: (id: string) => string }> = {
  contact_id: { label: "Contact", href: (id) => `/app/crm/contacts/${id}` },
  call_id: { label: "Call", href: (id) => `/app/calls/${id}` },
  thread_id: { label: "Email thread", href: (id) => `/app/inbox/threads/${id}` },
  campaign_id: { label: "Campaign", href: (id) => `/app/campaigns/calls/${id}` },
  agent_id: { label: "Agent", href: (id) => `/app/agents/${id}` },
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The verified context the request is bound to, with links only for well-formed ids. */
export function contextLinks(context: Record<string, unknown>): ContextLink[] {
  return Object.entries(context)
    .filter(([, v]) => typeof v === "string" && v.length > 0)
    .map(([k, v]) => {
      const value = v as string;
      const known = CONTEXT_LINKS[k];
      return {
        label: known?.label ?? k.replace(/_/g, " "),
        value,
        href: known && UUID.test(value) ? known.href(value) : null,
      };
    });
}

/** `pending` past its deadline is shown as expired even before the server sweeps it. */
export function effectiveStatus(a: Pick<Approval, "status" | "expires_at">, nowMs: number): string {
  if (a.status === "pending" && new Date(a.expires_at).getTime() <= nowMs) return "expired";
  return a.status;
}

export function executionMessage(out: DecisionOut): { tone: "success" | "error" | "info"; title: string; description?: string } {
  const ex = out.execution;
  if (!ex) return { tone: "success", title: "Approved", description: "The action runs once; its result appears in the history." };
  if (ex.status === "executed") return { tone: "success", title: "Approved and carried out" };
  return { tone: "error", title: `Approved, but the action did not run (${ex.status.replace(/_/g, " ")})`, description: ex.reason ?? undefined };
}

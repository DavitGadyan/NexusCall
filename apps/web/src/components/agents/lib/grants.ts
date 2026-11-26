/**
 * Tool grant editing rules (mcp_registry `PUT /api/v1/ai/grants`): class 4 can never be granted, class 3
 * always requires approval, a tenant- or platform-disabled tool is shown but not usable. The registry decides;
 * this only keeps the editor from offering what it would refuse.
 */
import type { Grant, Tool } from "./types";

export type GrantDraft = { tool_id: string; requires_approval: boolean; calls_per_minute: number | null };

/** build.md §24.3 tool risk classes. */
export const RISK_LABEL: Record<number, string> = {
  0: "Read-only",
  1: "Sensitive read",
  2: "Reversible write",
  3: "External side effect",
  4: "Privileged",
};

export function riskLabel(rc: number): string {
  return RISK_LABEL[rc] ?? `Class ${rc}`;
}

export function riskTone(rc: number): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (rc >= 4) return "danger";
  if (rc === 3) return "warn";
  if (rc === 2) return "brand";
  return "neutral";
}

export function canGrant(tool: Pick<Tool, "risk_class">): boolean {
  return tool.risk_class < 4;
}

export function approvalForced(tool: Pick<Tool, "risk_class" | "requires_approval">): boolean {
  return tool.risk_class >= 3 || tool.requires_approval;
}

export function draftsFromGrants(grants: readonly Grant[]): Map<string, GrantDraft> {
  return new Map(grants.map((g) => [g.tool_id, { tool_id: g.tool_id, requires_approval: g.requires_approval, calls_per_minute: g.calls_per_minute }]));
}

/** Toggle a tool in the draft set; class 4 is refused, class 3 is always added with approval on. */
export function toggleGrant(drafts: Map<string, GrantDraft>, tool: Tool, on: boolean): Map<string, GrantDraft> {
  const next = new Map(drafts);
  if (!on) {
    next.delete(tool.tool_id);
    return next;
  }
  if (!canGrant(tool)) return drafts;
  next.set(tool.tool_id, { tool_id: tool.tool_id, requires_approval: approvalForced(tool), calls_per_minute: null });
  return next;
}

export function setApproval(drafts: Map<string, GrantDraft>, tool: Tool, requires: boolean): Map<string, GrantDraft> {
  const cur = drafts.get(tool.tool_id);
  if (!cur) return drafts;
  const next = new Map(drafts);
  next.set(tool.tool_id, { ...cur, requires_approval: approvalForced(tool) ? true : requires });
  return next;
}

export function setRate(drafts: Map<string, GrantDraft>, toolId: string, raw: string): { drafts: Map<string, GrantDraft>; error?: string } {
  const cur = drafts.get(toolId);
  if (!cur) return { drafts };
  const v = raw.trim();
  let rate: number | null = null;
  if (v) {
    if (!/^\d+$/.test(v) || Number(v) < 1 || Number(v) > 6000) return { drafts, error: "1–6000 calls per minute, or empty for the default" };
    rate = Number(v);
  }
  const next = new Map(drafts);
  next.set(toolId, { ...cur, calls_per_minute: rate });
  return { drafts: next };
}

/** Body for PUT /api/v1/ai/grants?agent_id=…, sorted for a stable diff; class 4 entries are dropped. */
export function grantsBody(drafts: Map<string, GrantDraft>, tools: readonly Tool[]): { grants: GrantDraft[] } {
  const byId = new Map(tools.map((t) => [t.tool_id, t]));
  const grants = [...drafts.values()]
    .filter((d) => {
      const t = byId.get(d.tool_id);
      return !t || canGrant(t);
    })
    .map((d) => {
      const t = byId.get(d.tool_id);
      return { ...d, requires_approval: t && approvalForced(t) ? true : d.requires_approval };
    })
    .sort((a, b) => a.tool_id.localeCompare(b.tool_id));
  return { grants };
}

export function sameGrants(a: Map<string, GrantDraft>, b: Map<string, GrantDraft>): boolean {
  const key = (m: Map<string, GrantDraft>) => JSON.stringify([...m.values()].sort((x, y) => x.tool_id.localeCompare(y.tool_id)));
  return key(a) === key(b);
}

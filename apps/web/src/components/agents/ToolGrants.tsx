"use client";
/**
 * Tool grants of one agent against the tenant's tool catalogue. Class 4 tools can never be granted; class 3
 * tools always need human approval. The registry recomputes both on save and reports what is usable.
 */
import { ShieldAlert } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { Badge, Button, Card, CardHeader, Checkbox, EmptyState, ErrorState, Input, LoadingState, Notice, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "@/components/crm/common";
import { approvalForced, canGrant, draftsFromGrants, type GrantDraft, grantsBody, riskLabel, riskTone, sameGrants, setApproval, setRate, toggleGrant } from "./lib/grants";
import type { Agent, Grant, Tool } from "./lib/types";

export function ToolGrants({ agent }: { agent: Agent }) {
  const can = useCan();
  const toast = useToast();
  const tools = useApi<{ items: Tool[] }>("/api/v1/ai/tools?limit=200");
  const grants = useApi<{ agent_id: string; items: Grant[] }>(`/api/v1/ai/grants?agent_id=${agent.id}`);
  const [drafts, setDrafts] = React.useState<Map<string, GrantDraft>>(new Map());
  const [rateErrors, setRateErrors] = React.useState<Record<string, string>>({});
  const [rateText, setRateText] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const saved = React.useMemo(() => draftsFromGrants(grants.data?.items ?? []), [grants.data]);
  React.useEffect(() => {
    setDrafts(saved);
    setRateText(Object.fromEntries([...saved.values()].map((d) => [d.tool_id, d.calls_per_minute ? String(d.calls_per_minute) : ""])));
  }, [saved]);

  if ((tools.loading && !tools.data) || (grants.loading && !grants.data)) return <Card><LoadingState /></Card>;
  if (tools.error && !tools.data) return <Card><ErrorState problem={tools.error.problem} onRetry={tools.reload} /></Card>;
  if (grants.error && !grants.data) return <Card><ErrorState problem={grants.error.problem} onRetry={grants.reload} /></Card>;
  const catalogue = [...(tools.data?.items ?? [])].sort((a, b) => a.risk_class - b.risk_class || a.tool_id.localeCompare(b.tool_id));
  const grantInfo = new Map((grants.data?.items ?? []).map((g) => [g.tool_id, g]));
  const manage = can("ai:agents:manage") && agent.status !== "archived";
  const dirty = !sameGrants(drafts, saved);

  async function save() {
    if (Object.keys(rateErrors).length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ agent_id: string; items: Grant[] }>(`/api/v1/ai/grants?agent_id=${agent.id}`, { method: "PUT", body: grantsBody(drafts, catalogue) });
      grants.setData(() => out);
      const unusable = out.items.filter((g) => !g.usable);
      toast(unusable.length ? { tone: "info", title: "Grants saved", description: `${unusable.length} granted tool(s) are not usable right now.` } : { tone: "success", title: "Grants saved" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Tool grants"
        description="What this agent may call through the tool gateway. Every call is still checked against policy, rate limits and approvals at run time."
        actions={manage ? (
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" disabled={!dirty || busy} onClick={() => { setDrafts(saved); setRateErrors({}); }}>Reset</Button>
            <Button size="sm" disabled={!dirty || busy || Object.keys(rateErrors).length > 0} onClick={save} data-testid="save-grants">{busy ? "Saving…" : "Save grants"}</Button>
          </div>
        ) : null}
      />
      <div className="flex flex-col gap-3 px-5 pt-4">
        <Notice tone="info">
          Class 3 tools (external side effects) <strong>always need approval</strong> by a person. Class 4 (privileged) tools <strong>can never be granted</strong> to an agent.
        </Notice>
        {agent.kind === "email" ? <Notice tone="warn">Email drafting agents do not call tools; grants have no effect for them.</Notice> : null}
        <MutationError error={error} />
      </div>
      {catalogue.length === 0 ? <EmptyState title="No tools available" description="The platform has not enabled any tools for this workspace." /> : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Tool catalogue and grants</caption>
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th scope="col" className="border-b border-border px-4 py-2">Granted</th>
                <th scope="col" className="border-b border-border px-4 py-2">Tool</th>
                <th scope="col" className="border-b border-border px-4 py-2">Risk</th>
                <th scope="col" className="border-b border-border px-4 py-2">Approval</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Calls / min</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Status</th>
              </tr>
            </thead>
            <tbody>
              {catalogue.map((t) => {
                const d = drafts.get(t.tool_id);
                const info = grantInfo.get(t.tool_id);
                const grantable = canGrant(t);
                const forced = approvalForced(t);
                return (
                  <tr key={t.tool_id} className="align-top hover:bg-surface-2/60">
                    <td className="border-b border-border px-4 py-2">
                      <Checkbox
                        label={<span className="sr-only">Grant {t.display_name}</span>}
                        checked={Boolean(d)}
                        disabled={!manage || (!grantable && !d)}
                        onChange={(e) => setDrafts(toggleGrant(drafts, t, e.target.checked))}
                      />
                    </td>
                    <td className="border-b border-border px-4 py-2">
                      <p className="font-medium">{t.display_name}</p>
                      <code className="text-xs text-muted">{t.tool_id}</code>
                      {t.description ? <p className="mt-0.5 max-w-md text-xs text-muted">{t.description}</p> : null}
                    </td>
                    <td className="border-b border-border px-4 py-2">
                      <Badge tone={riskTone(t.risk_class)}>Class {t.risk_class}</Badge>
                      <span className="block text-xs text-muted">{riskLabel(t.risk_class)}{t.side_effecting ? " · side effects" : ""}</span>
                    </td>
                    <td className="border-b border-border px-4 py-2">
                      {!grantable ? (
                        <span className="inline-flex items-center gap-1 text-xs text-danger"><ShieldAlert className="h-3.5 w-3.5" aria-hidden /> Cannot be granted</span>
                      ) : forced ? (
                        <span className="text-xs font-medium text-warn">{t.risk_class >= 3 ? "Always needs approval" : "Needs approval (policy)"}</span>
                      ) : (
                        <Checkbox label="Require approval" checked={d?.requires_approval ?? false} disabled={!manage || !d} onChange={(e) => setDrafts(setApproval(drafts, t, e.target.checked))} />
                      )}
                    </td>
                    <td className="hidden border-b border-border px-4 py-2 md:table-cell">
                      {d ? (
                        <>
                          <label htmlFor={`rate-${t.tool_id}`} className="sr-only">Calls per minute for {t.display_name}</label>
                          <Input
                            id={`rate-${t.tool_id}`}
                            className="w-24"
                            inputMode="numeric"
                            placeholder={String(t.rate_per_minute)}
                            disabled={!manage}
                            value={rateText[t.tool_id] ?? ""}
                            aria-invalid={rateErrors[t.tool_id] ? true : undefined}
                            onChange={(e) => {
                              const raw = e.target.value;
                              setRateText((r) => ({ ...r, [t.tool_id]: raw }));
                              const res = setRate(drafts, t.tool_id, raw);
                              setDrafts(res.drafts);
                              setRateErrors((errs) => {
                                const next = { ...errs };
                                if (res.error) next[t.tool_id] = res.error;
                                else delete next[t.tool_id];
                                return next;
                              });
                            }}
                          />
                          {rateErrors[t.tool_id] ? <p className="mt-1 text-xs text-danger">{rateErrors[t.tool_id]}</p> : null}
                        </>
                      ) : <span className="text-xs text-muted">—</span>}
                    </td>
                    <td className="hidden border-b border-border px-4 py-2 md:table-cell">
                      {!t.platform_enabled ? <Badge tone="danger">Disabled by platform</Badge> : !t.enabled ? <Badge tone="warn">Disabled for workspace</Badge> : info && !info.usable ? <Badge tone="warn">{info.reason ? info.reason.replace(/_/g, " ") : "Not usable"}</Badge> : info ? <Badge tone="ok">Usable</Badge> : <span className="text-xs text-muted">Not granted</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

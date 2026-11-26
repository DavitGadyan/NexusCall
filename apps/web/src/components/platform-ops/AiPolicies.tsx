"use client";
/**
 * Tool policy with dual control: one administrator proposes, a different administrator approves, then the
 * approved proposal is deployed as a new version. Rollback re-activates an earlier deployed version as a new
 * version. Every step needs `platform:ai:manage` and a recent step-up and is audited.
 */
import { Check, Rocket, Undo2, X } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  ConfirmDialog,
  Dialog,
  ErrorState,
  Field,
  FormError,
  Input,
  KeyValues,
  LoadingState,
  LockedBadge,
  Notice,
  Select,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan, useSession } from "@/components/shell/session-context";
import { StepUpHint } from "./common";
import {
  formatPolicyValue,
  normalizePolicy,
  parseToolList,
  type Policies,
  policyDiff,
  policyLabel,
  type Proposal,
  proposalActions,
  proposalTone,
  type ToolPolicy,
  toPolicy,
  validatePolicy,
} from "./lib/ai";

export function AiPolicies() {
  const policies = useApi<Policies>("/api/v1/platform/ai/policies");
  const proposals = useApi<{ items: Proposal[] }>("/api/v1/platform/ai/policies/proposals");
  const reload = () => {
    policies.reload();
    proposals.reload();
  };

  if (policies.loading && !policies.data) return <Card><LoadingState label="Loading policy" /></Card>;
  if (policies.error && !policies.data) return <Card><ErrorState problem={policies.error.problem} onRetry={policies.reload} /></Card>;
  const p = policies.data;
  if (!p) return null;

  return (
    <div className="flex flex-col gap-4">
      <Notice tone="info" title="Dual approval">
        Every policy change is proposed by one administrator and must be approved by a <strong>different</strong> administrator before it can be deployed. You cannot approve your own proposal.
      </Notice>
      <Card>
        <CardHeader title={`Active policy · v${p.active_version}`} description="Applies to every tenant and agent. Tenants can only be stricter." />
        <div className="px-5 py-4">
          <KeyValues items={Object.entries(p.active).map(([k, v]) => ({ label: policyLabel(k), value: formatPolicyValue(v) }))} />
        </div>
      </Card>
      <ProposeCard key={p.active_version} active={p.active} onProposed={proposals.reload} />
      <ProposalsCard proposals={proposals} activeVersion={p.active_version} active={p.active} onChanged={reload} />
      <HistoryCard policies={p} onRolledBack={reload} />
    </div>
  );
}

function ProposeCard({ active, onProposed }: { active: Record<string, unknown>; onProposed: () => void }) {
  const can = useCan();
  const toast = useToast();
  const base = React.useMemo(() => toPolicy(active), [active]);
  const [draft, setDraft] = React.useState<ToolPolicy>(base);
  const [tools, setTools] = React.useState(base.disabled_tools.join("\n"));
  const [reason, setReason] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  if (!can("platform:ai:manage")) return null;

  const policy = normalizePolicy({ ...draft, disabled_tools: parseToolList(tools) });
  const diff = policyDiff(active, policy);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local = validatePolicy(policy);
    if (reason.trim().length < 10) local.reason = "At least 10 characters: why is this change needed?";
    if (diff.length === 0) local._form = "The proposal does not change anything.";
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/platform/ai/policies/proposals", { method: "POST", body: { policy, reason: reason.trim() } });
      toast({ tone: "success", title: "Proposal submitted", description: "Another administrator must approve it." });
      setReason("");
      onProposed();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Propose a change" description="Edit the values, give a reason, and submit for a second administrator's approval." actions={<StepUpHint />} />
      <form onSubmit={submit} className="flex flex-col gap-4 px-5 py-4" noValidate>
        <div className="grid gap-4 md:grid-cols-2">
          <Field id="pol-risk" label={policyLabel("max_agent_risk_class")} hint="Class 4 tools can never be granted to agents." error={errors.max_agent_risk_class}>
            {(p) => (
              <Select {...p} value={String(draft.max_agent_risk_class)} onChange={(e) => setDraft((d) => ({ ...d, max_agent_risk_class: Number(e.target.value) }))} disabled={busy}>
                {[0, 1, 2, 3].map((n) => (
                  <option key={n} value={n}>
                    Class {n}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="pol-rate" label={policyLabel("agent_calls_per_minute")} hint="1–6000" error={errors.agent_calls_per_minute}>
            {(p) => (
              <Input
                {...p}
                inputMode="numeric"
                value={String(draft.agent_calls_per_minute)}
                onChange={(e) => /^\d{0,4}$/.test(e.target.value) && setDraft((d) => ({ ...d, agent_calls_per_minute: Number(e.target.value || 0) }))}
                disabled={busy}
              />
            )}
          </Field>
        </div>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">{policyLabel("approval_required_classes")}</legend>
          <div className="flex flex-wrap items-center gap-4">
            {[0, 1, 2, 3].map((c) => (
              <Checkbox
                key={c}
                label={`Class ${c}`}
                checked={draft.approval_required_classes.includes(c) || c === 3}
                disabled={busy || c === 3}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, approval_required_classes: e.target.checked ? [...d.approval_required_classes, c] : d.approval_required_classes.filter((x) => x !== c) }))
                }
              />
            ))}
            <LockedBadge reason="Class 3 tools always require human approval." />
          </div>
          {errors.approval_required_classes ? <p className="text-xs text-danger">{errors.approval_required_classes}</p> : null}
        </fieldset>
        <div className="flex flex-wrap gap-6">
          <Checkbox label={policyLabel("mcp_tools_enabled")} checked={draft.mcp_tools_enabled} onChange={(e) => setDraft((d) => ({ ...d, mcp_tools_enabled: e.target.checked }))} disabled={busy} />
          <span className="flex items-center gap-2">
            <Checkbox label={policyLabel("mcp_require_pinned")} checked disabled />
            <LockedBadge reason="MCP servers must always be pinned to a reviewed version." />
          </span>
        </div>
        <Field id="pol-tools" label={policyLabel("disabled_tools")} hint="Tool ids, one per line." error={errors.disabled_tools}>
          {(p) => <Textarea {...p} value={tools} onChange={(e) => setTools(e.target.value)} rows={3} disabled={busy} className="font-mono text-xs" spellCheck={false} />}
        </Field>
        <Field id="pol-reason" label="Reason" hint="Shown to the approver and kept in the audit log." required error={errors.reason}>
          {(p) => <Textarea {...p} value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={2000} disabled={busy} />}
        </Field>
        <DiffList diff={diff} />
        <FormError message={errors._form ?? (error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null)} />
        <div className="flex justify-end">
          <Button type="submit" disabled={busy || diff.length === 0} data-testid="propose-policy">
            {busy ? "Submitting…" : "Submit proposal"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

function DiffList({ diff }: { diff: { key: string; before: string; after: string }[] }) {
  if (diff.length === 0) return <p className="text-sm text-muted">No changes from the active policy.</p>;
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Changes</caption>
        <thead>
          <tr className="text-xs uppercase tracking-wide text-muted">
            <th scope="col" className="border-b border-border px-3 py-1.5">Setting</th>
            <th scope="col" className="border-b border-border px-3 py-1.5">Now</th>
            <th scope="col" className="border-b border-border px-3 py-1.5">Proposed</th>
          </tr>
        </thead>
        <tbody>
          {diff.map((d) => (
            <tr key={d.key}>
              <td className="border-b border-border px-3 py-1.5">{policyLabel(d.key)}</td>
              <td className="border-b border-border px-3 py-1.5 font-mono text-xs text-muted line-through">{d.before}</td>
              <td className="border-b border-border px-3 py-1.5 font-mono text-xs">{d.after}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Decision = { proposal: Proposal; action: "approve" | "reject" };

function ProposalsCard({
  proposals,
  activeVersion,
  active,
  onChanged,
}: {
  proposals: { data: { items: Proposal[] } | undefined; loading: boolean; error: ApiError | undefined; reload: () => void };
  activeVersion: number;
  active: Record<string, unknown>;
  onChanged: () => void;
}) {
  const can = useCan();
  const me = useSession().user.id;
  const toast = useToast();
  const [deciding, setDeciding] = React.useState<Decision | null>(null);
  const [deploying, setDeploying] = React.useState<Proposal | null>(null);
  const canManage = can("platform:ai:manage");
  const items = proposals.data?.items ?? [];

  return (
    <Card>
      <CardHeader title="Proposals" description="Pending proposals expire if no one decides in time." />
      {proposals.loading && !proposals.data ? (
        <LoadingState label="Loading proposals" />
      ) : proposals.error && !proposals.data ? (
        <ErrorState problem={proposals.error.problem} onRetry={proposals.reload} />
      ) : items.length === 0 ? (
        <p className="px-5 py-6 text-sm text-muted">No proposals yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((p) => {
            const a = proposalActions(p, me, activeVersion);
            const mine = p.proposed_by === me;
            return (
              <li key={p.id} className="flex flex-col gap-3 px-5 py-4" data-testid="proposal">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={proposalTone(p.status)}>{p.status}</Badge>
                  <span className="text-sm">
                    Proposed by <span className="font-mono text-xs">{shortId(p.proposed_by)}</span>
                    {mine ? <span className="ml-1"><Badge tone="brand">you</Badge></span> : null} on v{p.base_version} · {relativeTime(p.created_at)}
                  </span>
                  {p.status === "pending" ? <span className="ml-auto text-xs text-muted">Expires {formatDateTime(p.expires_at)}</span> : null}
                  {p.deployed_version ? <span className="ml-auto text-xs text-muted">Deployed as v{p.deployed_version}</span> : null}
                </div>
                <p className="text-sm">{p.reason}</p>
                {p.status === "pending" || p.status === "approved" ? (
                  <DiffList diff={policyDiff(active, p.policy)} />
                ) : (
                  <details className="text-sm">
                    <summary className="cursor-pointer text-xs text-muted">Proposed policy</summary>
                    <KeyValues className="mt-2" items={Object.entries(p.policy).map(([k, v]) => ({ label: policyLabel(k), value: formatPolicyValue(v) }))} />
                  </details>
                )}
                {p.approved_by ? <p className="text-xs text-muted">Approved by {shortId(p.approved_by)}{p.decided_at ? ` · ${formatDateTime(p.decided_at)}` : ""}</p> : null}
                {p.rejected_by ? <p className="text-xs text-muted">Rejected by {shortId(p.rejected_by)}{p.decided_at ? ` · ${formatDateTime(p.decided_at)}` : ""}</p> : null}
                {p.decision_note ? <p className="text-xs text-muted">Note: {p.decision_note}</p> : null}
                {canManage && (a.approve || a.reject || a.deploy || a.approveNote || a.deployNote) ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {a.approve ? (
                      <Button size="sm" onClick={() => setDeciding({ proposal: p, action: "approve" })} data-testid="approve-proposal">
                        <Check className="h-4 w-4" aria-hidden /> Approve
                      </Button>
                    ) : null}
                    {a.deploy ? (
                      <Button size="sm" onClick={() => setDeploying(p)} data-testid="deploy-proposal">
                        <Rocket className="h-4 w-4" aria-hidden /> Deploy
                      </Button>
                    ) : null}
                    {a.reject ? (
                      <Button size="sm" variant="secondary" onClick={() => setDeciding({ proposal: p, action: "reject" })}>
                        <X className="h-4 w-4" aria-hidden /> Reject
                      </Button>
                    ) : null}
                    {a.approveNote ? <span className="text-xs text-muted" data-testid="approve-note">{a.approveNote}</span> : null}
                    {a.deployNote ? <span className="text-xs text-warn">{a.deployNote}</span> : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {deciding ? <DecisionDialog decision={deciding} onClose={() => setDeciding(null)} onDone={onChanged} /> : null}
      <ConfirmDialog
        open={deploying !== null}
        onClose={() => setDeploying(null)}
        title="Deploy policy"
        description={deploying ? `The approved proposal becomes the active policy (v${activeVersion + 1}) for every tenant and agent.` : undefined}
        confirmLabel="Deploy"
        tone="primary"
        typedConfirmation="deploy"
        onConfirm={async () => {
          if (!deploying) return;
          await api(`/api/v1/platform/ai/policies/proposals/${deploying.id}/deploy`, { method: "POST" });
          toast({ tone: "success", title: "Policy deployed" });
          onChanged();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </Card>
  );
}

function DecisionDialog({ decision, onClose, onDone }: { decision: Decision; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const approve = decision.action === "approve";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/policies/proposals/${decision.proposal.id}/${decision.action}`, { method: "POST", body: { note: note.trim() } });
      toast({ tone: "success", title: approve ? "Proposal approved" : "Proposal rejected" });
      onDone();
      onClose();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title={approve ? "Approve proposal" : "Reject proposal"} description={decision.proposal.reason} size="md">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <StepUpHint />
        {approve ? <p className="text-sm text-muted">You are the second administrator for this change. After approval, it still has to be deployed.</p> : null}
        <Field id="decision-note" label="Note" hint="Optional, kept with the decision.">
          {(p) => <Textarea {...p} value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} disabled={busy} />}
        </Field>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant={approve ? "primary" : "danger"} disabled={busy}>
            {busy ? "Saving…" : approve ? "Approve" : "Reject"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function HistoryCard({ policies, onRolledBack }: { policies: Policies; onRolledBack: () => void }) {
  const can = useCan();
  const toast = useToast();
  const [target, setTarget] = React.useState<number | null>(null);
  const canManage = can("platform:ai:manage");
  return (
    <Card>
      <CardHeader title="Version history" description="Deployed versions, newest first. Rolling back re-activates an earlier version as a new version." />
      <ul className="divide-y divide-border">
        {policies.items.map((v) => (
          <li key={v.version} className="flex flex-wrap items-center gap-3 px-5 py-2.5 text-sm">
            <span className="tabular w-10 font-medium">v{v.version}</span>
            <Badge tone={v.status === "active" ? "ok" : "neutral"}>{v.status}</Badge>
            {v.rollback_of ? <span className="text-xs text-muted">rollback of v{v.rollback_of}</span> : null}
            {v.proposal_id ? <span className="font-mono text-xs text-muted">proposal {shortId(v.proposal_id)}</span> : null}
            <span className="text-xs text-muted">
              {formatDateTime(v.deployed_at)} · {v.deployed_by}
            </span>
            {canManage && v.version !== policies.active_version ? (
              <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setTarget(v.version)}>
                <Undo2 className="h-4 w-4" aria-hidden /> Roll back to v{v.version}
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={target !== null}
        onClose={() => setTarget(null)}
        title={`Roll back to v${target ?? ""}`}
        description="The earlier, previously approved policy becomes active again as a new version. Pending proposals based on the current version can no longer be deployed."
        confirmLabel="Roll back"
        typedConfirmation={target !== null ? `rollback v${target}` : undefined}
        onConfirm={async () => {
          if (target === null) return;
          await api(`/api/v1/platform/ai/policies/versions/${target}/rollback`, { method: "POST" });
          toast({ tone: "success", title: `Rolled back to v${target}` });
          onRolledBack();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </Card>
  );
}

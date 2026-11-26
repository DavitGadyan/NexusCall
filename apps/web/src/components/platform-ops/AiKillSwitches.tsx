"use client";
/**
 * AI kill switches (platform, provider, model, tenant or agent). Engaging or releasing one needs
 * `platform:ai:manage`, a recent step-up, a reason and a typed confirmation that names the switch.
 */
import { OctagonX, Power } from "lucide-react";
import * as React from "react";
import { api } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, shortId } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, ConfirmDialog, DataTable, EmptyState, ErrorState, Field, Input, LoadingState, Notice, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { StepUpHint } from "./common";
import { KILL_SCOPES, type KillSwitch, killPhrase, validateKillSwitch } from "./lib/ai";

type Pending = { scope: string; target: string; engage: boolean };

export function AiKillSwitches() {
  const can = useCan();
  const toast = useToast();
  const list = useApi<{ items: KillSwitch[] }>("/api/v1/platform/ai/kill-switches");
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [scope, setScope] = React.useState("model");
  const [target, setTarget] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const canManage = can("platform:ai:manage");
  const engaged = (list.data?.items ?? []).filter((k) => k.engaged);

  function startNew(e: React.FormEvent) {
    e.preventDefault();
    const t = scope === "platform" ? "*" : target.trim().toLowerCase();
    const errs = validateKillSwitch({ scope, target: t, reason: "placeholder" });
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setPending({ scope, target: t, engage: true });
  }

  const columns: Column<KillSwitch>[] = [
    { key: "scope", header: "Scope", cell: (k) => KILL_SCOPES.find((s) => s.value === k.scope)?.label ?? k.scope },
    { key: "target", header: "Target", cell: (k) => <span className="font-mono text-xs">{k.scope === "tenant" || k.scope === "agent" ? shortId(k.target) : k.target}</span> },
    { key: "state", header: "State", cell: (k) => <Badge tone={k.engaged ? "danger" : "neutral"}>{k.engaged ? "Engaged" : "Released"}</Badge> },
    { key: "reason", header: "Reason", hideOnMobile: true, cell: (k) => <span className="line-clamp-2 max-w-sm text-xs">{k.reason}</span> },
    { key: "updated", header: "Changed", hideOnMobile: true, cell: (k) => formatDateTime(k.updated_at) },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (k) =>
        canManage ? (
          <Button size="sm" variant={k.engaged ? "secondary" : "danger"} onClick={() => setPending({ scope: k.scope, target: k.target, engage: !k.engaged })}>
            <Power className="h-4 w-4" aria-hidden /> {k.engaged ? "Release" : "Engage"}
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {engaged.length > 0 ? (
        <Notice tone="danger" title={`${engaged.length} kill switch${engaged.length === 1 ? "" : "es"} engaged`}>
          Matching AI requests are refused until the switch is released.
        </Notice>
      ) : null}
      {canManage ? (
        <Card>
          <CardHeader title="Engage a kill switch" description="Stops matching AI requests immediately. Use the narrowest scope that contains the problem." actions={<StepUpHint />} />
          <form onSubmit={startNew} className="flex flex-wrap items-start gap-4 px-5 py-4" noValidate>
            <Field id="ks-scope" label="Scope" hint={KILL_SCOPES.find((s) => s.value === scope)?.hint} error={errors.scope} className="w-56">
              {(p) => (
                <Select {...p} value={scope} onChange={(e) => setScope(e.target.value)}>
                  {KILL_SCOPES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field id="ks-target" label="Target" error={errors.target} className="min-w-[16rem] flex-1">
              {(p) => <Input {...p} value={scope === "platform" ? "*" : target} disabled={scope === "platform"} onChange={(e) => setTarget(e.target.value)} className="font-mono" spellCheck={false} />}
            </Field>
            <div className="pt-6">
              <Button type="submit" variant="danger" data-testid="engage-kill-switch">
                <OctagonX className="h-4 w-4" aria-hidden /> Engage…
              </Button>
            </div>
          </form>
        </Card>
      ) : null}
      <Card>
        <CardHeader title="Kill switches" description="Every change is recorded in the audit log with its reason." />
        {list.loading && !list.data ? (
          <LoadingState label="Loading kill switches" />
        ) : list.error && !list.data ? (
          <ErrorState problem={list.error.problem} onRetry={list.reload} />
        ) : (
          <DataTable caption="AI kill switches" columns={columns} rows={list.data?.items ?? []} rowKey={(k) => `${k.scope}:${k.target}`} empty={<EmptyState title="No kill switches have been set" />} />
        )}
      </Card>
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending?.engage ? "Engage kill switch" : "Release kill switch"}
        description={
          pending
            ? pending.engage
              ? `AI requests matching ${pending.scope}:${pending.target} will be refused immediately.`
              : `AI requests matching ${pending.scope}:${pending.target} will be allowed again.`
            : undefined
        }
        confirmLabel={pending?.engage ? "Engage" : "Release"}
        tone={pending?.engage ? "danger" : "primary"}
        typedConfirmation={pending ? killPhrase(pending.scope, pending.target, pending.engage) : undefined}
        reason={{ minLength: 5, maxLength: 300 }}
        onConfirm={async (reason) => {
          if (!pending) return;
          await api("/api/v1/platform/ai/kill-switches", { method: "POST", body: { scope: pending.scope, target: pending.target, engaged: pending.engage, reason } });
          toast({ tone: "success", title: pending.engage ? "Kill switch engaged" : "Kill switch released" });
          setTarget("");
          list.reload();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </div>
  );
}

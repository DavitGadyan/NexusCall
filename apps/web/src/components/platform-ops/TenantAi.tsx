"use client";
/**
 * Tenant AI settings: the workspace's AI and approval-rule settings (versioned, through the shared settings
 * editor), the models the platform makes available, budget and usage, tool settings and AI security
 * events. Anything fixed by the platform is shown locked; tenants can only tighten platform limits.
 */
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { type CursorListState, useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import { TENANT_SETTINGS } from "@/lib/settings-descriptors";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  Input,
  LoadingState,
  LockedBadge,
  Notice,
  Select,
  useToast,
} from "@/components/ui";
import { SettingsEditor } from "@/components/settings/SettingsEditor";
import { useCan } from "@/components/shell/session-context";
import { KpiTile, Meter } from "@/components/analytics/charts";
import { recentPeriods } from "@/components/analytics/lib/dates";
import { formatCompact, formatInt, formatUsd } from "@/components/analytics/lib/format";
import { type SecurityEvent, severityTone } from "./lib/ai";
import { riskInfo } from "./lib/mcp";

const SECTIONS = TENANT_SETTINGS.filter((s) => s.id === "ai" || s.id === "approval");

export function TenantAi({ initialTab }: { initialTab?: string }) {
  const can = useCan();
  const extraTabs = [
    ...(can("ai:agents:read") ? [{ id: "models", label: "Available models", content: <ModelsTab /> }] : []),
    ...(can("ai:usage:read") ? [{ id: "budget", label: "Budget & usage", content: <BudgetTab /> }] : []),
    ...(can("ai:agents:read") ? [{ id: "tools", label: "Tools", content: <ToolsTab /> }] : []),
    ...(can("audit:read") ? [{ id: "security", label: "Security events", content: <SecurityTab /> }] : []),
  ];
  return <SettingsEditor endpoint="/api/v1/settings" sections={SECTIONS} readOnly={!can("tenant:manage")} initialTab={initialTab} extraTabs={extraTabs} />;
}

type TenantModel = { model_id: string; display_name: string; provider: string; purposes: string[]; allowed_classifications: string[]; platform_restricted: boolean };

function ModelsTab() {
  const models = useApi<{ items: TenantModel[] }>("/api/v1/ai/models");
  const columns: Column<TenantModel>[] = [
    {
      key: "model",
      header: "Model",
      cell: (m) => (
        <span>
          <span className="font-medium">{m.display_name}</span>
          <span className="block font-mono text-xs text-muted">{m.model_id}</span>
        </span>
      ),
    },
    { key: "provider", header: "Provider", hideOnMobile: true, cell: (m) => m.provider },
    { key: "purposes", header: "Used for", hideOnMobile: true, cell: (m) => <span className="text-xs">{m.purposes.join(", ").replace(/_/g, " ") || "—"}</span> },
    { key: "classes", header: "Data classes allowed", cell: (m) => <span className="text-xs">{m.allowed_classifications.join(", ")}</span> },
    {
      key: "available",
      header: "Availability",
      cell: (m) => (m.platform_restricted ? <LockedBadge reason="The platform has not allowed this model for this workspace." /> : <Badge tone="ok">Available</Badge>),
    },
  ];
  return (
    <Card>
      <CardHeader
        title="Models the platform enables"
        description="Choose which of these your workspace may use in the AI tab. Models locked by the platform cannot be selected."
      />
      {models.loading && !models.data ? (
        <LoadingState label="Loading models" />
      ) : models.error && !models.data ? (
        <ErrorState problem={models.error.problem} onRetry={models.reload} />
      ) : (
        <DataTable caption="Models" columns={columns} rows={models.data?.items ?? []} rowKey={(m) => m.model_id} empty={<EmptyState title="No models are enabled on the platform" />} />
      )}
    </Card>
  );
}

type Usage = {
  period: string;
  total: { requests: number; input_tokens: number; output_tokens: number; cost_micros: number; denied: number };
  by_agent: { agent_key: string; requests: number; input_tokens: number; output_tokens: number; cost_micros: number; denied: number }[];
  budgets: { scope: string; agent_id: string | null; monthly_limit_micros: number; per_request_limit_micros: number | null; version: number }[];
};

function BudgetTab() {
  const can = useCan();
  const periods = React.useMemo(() => recentPeriods(6), []);
  const [period, setPeriod] = React.useState(periods[0] ?? "");
  const usage = useApi<Usage>(withQuery("/api/v1/ai/usage", { period }));
  const u = usage.data;
  const tenantBudget = u?.budgets.find((b) => b.scope === "tenant") ?? null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-muted">
          Period (UTC month)
          <Select value={period} onChange={(e) => setPeriod(e.target.value)} className="w-40">
            {periods.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
        </label>
      </div>
      {usage.loading && !u ? (
        <Card>
          <LoadingState label="Loading usage" />
        </Card>
      ) : usage.error && !u ? (
        <Card>
          <ErrorState problem={usage.error.problem} onRetry={usage.reload} />
        </Card>
      ) : u ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <KpiTile label="AI cost" value={formatUsd(u.total.cost_micros)} />
            <KpiTile label="Requests" value={formatCompact(u.total.requests)} hint={`${formatInt(u.total.denied)} denied by policy or budget`} />
            <KpiTile label="Input tokens" value={formatCompact(u.total.input_tokens)} />
            <KpiTile label="Output tokens" value={formatCompact(u.total.output_tokens)} />
          </div>
          <Card>
            <CardHeader title="Monthly budget" description="The effective limit is the lower of this budget and the platform's maximum. Requests over budget are denied." />
            <div className="flex flex-col gap-4 px-5 py-4">
              {tenantBudget ? (
                <Meter label={`Spend in ${u.period}`} value={u.total.cost_micros} limit={tenantBudget.monthly_limit_micros} format={(v) => formatUsd(v)} />
              ) : (
                <p className="text-sm text-muted">No workspace budget policy is set; the monthly budget in the AI tab and the platform maximum apply.</p>
              )}
              {can("tenant:manage") ? <BudgetForm current={tenantBudget} onSaved={usage.reload} /> : null}
            </div>
          </Card>
          <Card>
            <CardHeader title="By agent" description="Usage this period per AI agent (requests without an agent are grouped)." />
            <DataTable
              caption="AI usage by agent"
              rows={u.by_agent}
              rowKey={(r) => r.agent_key}
              columns={[
                { key: "agent", header: "Agent", cell: (r) => <span className="font-mono text-xs">{r.agent_key === "-" ? "No agent" : shortId(r.agent_key)}</span> },
                { key: "requests", header: "Requests", cell: (r) => <span className="tabular">{formatInt(r.requests)}</span> },
                { key: "tokens", header: "Tokens in / out", hideOnMobile: true, cell: (r) => <span className="tabular">{formatCompact(r.input_tokens)} / {formatCompact(r.output_tokens)}</span> },
                { key: "denied", header: "Denied", hideOnMobile: true, cell: (r) => <span className="tabular">{formatInt(r.denied)}</span> },
                { key: "cost", header: "Cost", cell: (r) => <span className="tabular">{formatUsd(r.cost_micros)}</span> },
              ]}
              empty={<EmptyState title="No AI usage in this period" />}
            />
          </Card>
        </>
      ) : null}
    </div>
  );
}

function BudgetForm({ current, onSaved }: { current: Usage["budgets"][number] | null; onSaved: () => void }) {
  const toast = useToast();
  const [monthly, setMonthly] = React.useState(current ? String(Math.floor(current.monthly_limit_micros / 1_000_000)) : "");
  const [perRequest, setPerRequest] = React.useState(current?.per_request_limit_micros ? String(current.per_request_limit_micros / 1_000_000) : "");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local: Record<string, string> = {};
    const m = /^\d{1,7}$/.test(monthly.trim()) ? Number(monthly.trim()) : NaN;
    if (!Number.isInteger(m) || m < 0 || m > 1_000_000) local.monthly_limit_usd = "A whole number of dollars, 0–1,000,000.";
    let pr: number | null = null;
    if (perRequest.trim()) {
      pr = Number(perRequest.trim());
      if (!Number.isFinite(pr) || pr <= 0 || pr > 100) local.per_request_limit_usd = "More than 0 and at most 100.";
    }
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/ai/budgets", { method: "PUT", body: { monthly_limit_usd: m, per_request_limit_usd: pr } });
      toast({ tone: "success", title: "Budget saved" });
      onSaved();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-4 border-t border-border pt-4" noValidate>
      <Field id="budget-monthly" label="Monthly limit (USD)" error={errors.monthly_limit_usd} className="w-48">
        {(p) => <Input {...p} inputMode="numeric" value={monthly} onChange={(e) => setMonthly(e.target.value)} disabled={busy} />}
      </Field>
      <Field id="budget-request" label="Per-request limit (USD)" hint="Optional." error={errors.per_request_limit_usd} className="w-48">
        {(p) => <Input {...p} inputMode="decimal" value={perRequest} onChange={(e) => setPerRequest(e.target.value)} disabled={busy} />}
      </Field>
      <Button type="submit" disabled={busy}>
        {busy ? "Saving…" : "Save budget"}
      </Button>
      <div className="w-full">
        <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
      </div>
    </form>
  );
}

type ToolSetting = {
  tool_id: string;
  risk_class: number;
  enabled: boolean;
  requires_approval: boolean;
  rate_per_minute: number | null;
  platform_enabled: boolean;
  platform_requires_approval: boolean;
  platform_rate_per_minute: number;
};

type ToolInfo = { tool_id: string; display_name: string; description: string };

function ToolsTab() {
  const can = useCan();
  const toast = useToast();
  const settings = useApi<{ items: ToolSetting[] }>("/api/v1/ai/tools/settings");
  const tools = useCursorList<ToolInfo>("/api/v1/ai/tools", 200);
  const names = React.useMemo(() => new Map(tools.items.map((t) => [t.tool_id, t])), [tools.items]);
  const [draft, setDraft] = React.useState<Record<string, ToolSetting>>({});
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const canManage = can("tenant:manage");

  const rows = (settings.data?.items ?? []).map((s) => draft[s.tool_id] ?? s);
  const dirty = Object.keys(draft).length > 0;

  function update(id: string, patch: Partial<ToolSetting>) {
    const base = draft[id] ?? settings.data?.items.find((s) => s.tool_id === id);
    if (!base) return;
    setDraft((d) => ({ ...d, [id]: { ...base, ...patch } }));
  }

  async function save() {
    const local: Record<string, string> = {};
    for (const t of Object.values(draft)) {
      if (t.rate_per_minute !== null && (!Number.isInteger(t.rate_per_minute) || t.rate_per_minute < 1 || t.rate_per_minute > t.platform_rate_per_minute))
        local[t.tool_id] = `Between 1 and the platform limit of ${t.platform_rate_per_minute}.`;
    }
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ items: ToolSetting[] }>("/api/v1/ai/tools/settings", {
        method: "PUT",
        body: {
          settings: Object.values(draft).map((t) => ({ tool_id: t.tool_id, enabled: t.enabled, requires_approval: t.requires_approval, rate_per_minute: t.rate_per_minute })),
        },
      });
      settings.setData(() => out);
      setDraft({});
      toast({ tone: "success", title: "Tool settings saved" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<ToolSetting>[] = [
    {
      key: "tool",
      header: "Tool",
      cell: (t) => (
        <span>
          <span className="font-medium">{names.get(t.tool_id)?.display_name ?? t.tool_id}</span>
          <span className="block font-mono text-xs text-muted">{t.tool_id}</span>
        </span>
      ),
    },
    {
      key: "risk",
      header: "Risk class",
      hideOnMobile: true,
      cell: (t) => {
        const r = riskInfo(t.risk_class);
        return (
          <span title={r.help}>
            <Badge tone={r.tone}>{r.label}</Badge>
          </span>
        );
      },
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (t) =>
        !t.platform_enabled ? (
          <LockedBadge reason="Disabled by the platform." />
        ) : (
          <Checkbox label={<span className="sr-only">Enable {t.tool_id}</span>} checked={t.enabled} disabled={!canManage || busy} onChange={(e) => update(t.tool_id, { enabled: e.target.checked })} />
        ),
    },
    {
      key: "approval",
      header: "Needs approval",
      cell: (t) =>
        t.platform_requires_approval || t.risk_class >= 3 ? (
          <span className="flex items-center gap-2">
            <Badge tone="warn">Always</Badge> <LockedBadge reason="The platform requires human approval for this tool." />
          </span>
        ) : (
          <Checkbox
            label={<span className="sr-only">Require approval for {t.tool_id}</span>}
            checked={t.requires_approval}
            disabled={!canManage || busy}
            onChange={(e) => update(t.tool_id, { requires_approval: e.target.checked })}
          />
        ),
    },
    {
      key: "rate",
      header: "Calls / minute",
      cell: (t) => (
        <span className="flex flex-col gap-0.5">
          <Input
            aria-label={`Rate limit for ${t.tool_id}`}
            inputMode="numeric"
            placeholder={String(t.platform_rate_per_minute)}
            value={t.rate_per_minute === null ? "" : String(t.rate_per_minute)}
            disabled={!canManage || busy}
            onChange={(e) => {
              const v = e.target.value.trim();
              if (v === "") update(t.tool_id, { rate_per_minute: null });
              else if (/^\d{1,5}$/.test(v)) update(t.tool_id, { rate_per_minute: Number(v) });
            }}
            className="h-8 w-24"
            aria-invalid={errors[t.tool_id] ? true : undefined}
          />
          <span className="text-xs text-muted">Platform max {t.platform_rate_per_minute}</span>
          {errors[t.tool_id] ? <span className="text-xs text-danger">{errors[t.tool_id]}</span> : null}
        </span>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Tools"
        description="Turn tools off for this workspace or require approval more often than the platform does. Per-agent grants are set on each AI agent."
        actions={
          canManage ? (
            <div className="flex gap-2">
              <Button variant="secondary" disabled={!dirty || busy} onClick={() => setDraft({})}>
                Discard
              </Button>
              <Button disabled={!dirty || busy} onClick={save}>
                {busy ? "Saving…" : "Save changes"}
              </Button>
            </div>
          ) : null
        }
      />
      {error && !error.isStepUp ? (
        <div className="px-5 pt-4">
          <FormError message={error.message} />
        </div>
      ) : null}
      {!canManage ? (
        <div className="px-5 pt-4">
          <Notice tone="info">Changing tool settings needs the tenant:manage permission.</Notice>
        </div>
      ) : null}
      {settings.loading && !settings.data ? (
        <LoadingState label="Loading tools" />
      ) : settings.error && !settings.data ? (
        <ErrorState problem={settings.error.problem} onRetry={settings.reload} />
      ) : (
        <DataTable caption="Tool settings" columns={columns} rows={rows} rowKey={(t) => t.tool_id} empty={<EmptyState title="No tools are available to this workspace" />} />
      )}
    </Card>
  );
}

function SecurityTab() {
  const list = useCursorList<SecurityEvent>("/api/v1/ai/security-events");
  return (
    <Card>
      <CardHeader title="AI security events" description="Prompt-injection attempts, blocked outputs and denied tool calls in this workspace. No prompt or completion text is stored." />
      <SecurityEventsTable list={list} showTenant={false} />
    </Card>
  );
}

export function SecurityEventsTable({ list, showTenant }: { list: CursorListState<SecurityEvent>; showTenant: boolean }) {
  const columns: Column<SecurityEvent>[] = [
    { key: "severity", header: "Severity", cell: (e) => <Badge tone={severityTone(e.severity)}>{e.severity}</Badge> },
    {
      key: "kind",
      header: "Event",
      cell: (e) => (
        <span>
          <span className="font-medium">{e.kind.replace(/_/g, " ")}</span>
          <span className="block text-xs text-muted">OWASP {e.owasp}</span>
        </span>
      ),
    },
    // `detail` is a short service-written summary, rendered as text (never as HTML).
    { key: "detail", header: "Detail", hideOnMobile: true, cell: (e) => <span className="line-clamp-2 max-w-md text-xs">{e.detail || "—"}</span> },
    ...(showTenant ? [{ key: "tenant", header: "Tenant", hideOnMobile: true, cell: (e: SecurityEvent) => <span className="font-mono text-xs">{shortId(e.tenant_id)}</span> }] : []),
    {
      key: "ref",
      header: "Agent / tool",
      hideOnMobile: true,
      cell: (e) => <span className="font-mono text-xs">{[e.agent_id ? shortId(e.agent_id) : null, e.tool_id].filter(Boolean).join(" · ") || "—"}</span>,
    },
    { key: "when", header: "When", cell: (e) => <span title={formatDateTime(e.created_at)}>{relativeTime(e.created_at)}</span> },
  ];
  return (
    <DataTable
      caption="AI security events"
      columns={columns}
      rows={list.items}
      rowKey={(e) => e.id}
      loading={list.loading}
      error={list.error}
      onRetry={list.reload}
      hasMore={list.hasMore}
      loadingMore={list.loadingMore}
      onLoadMore={list.loadMore}
      empty={<EmptyState title="No AI security events" />}
    />
  );
}

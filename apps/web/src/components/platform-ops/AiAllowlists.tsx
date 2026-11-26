"use client";
/**
 * Per-tenant model allowlists. An empty list means no platform restriction (the tenant's own settings
 * still apply). Saving replaces the tenant's list; it needs `platform:ai:manage` and a recent step-up.
 */
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import type { Page, Tenant } from "@/lib/types";
import { Button, Card, CardHeader, Checkbox, ErrorState, Field, FormError, LoadingState, Notice, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { StepUpHint } from "./common";
import type { AiModel } from "./lib/ai";

export function AiAllowlists() {
  const can = useCan();
  const toast = useToast();
  const tenants = useApi<Page<Tenant>>("/api/v1/platform/tenants?status=active");
  const models = useApi<{ models: AiModel[] }>("/api/v1/platform/ai/models");
  const [tenantId, setTenantId] = React.useState("");
  const [selected, setSelected] = React.useState<string[]>([]);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [saved, setSaved] = React.useState<Record<string, string[]>>({});
  const canManage = can("platform:ai:manage");

  React.useEffect(() => {
    setSelected(saved[tenantId] ?? []);
    setError(null);
  }, [tenantId, saved]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!tenantId) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ tenant_id: string; models: string[] }>(`/api/v1/platform/ai/tenants/${tenantId}/models`, { method: "PUT", body: { models: selected } });
      setSaved((s) => ({ ...s, [tenantId]: out.models }));
      toast({ tone: "success", title: out.models.length ? `Allowlist saved (${out.models.length} models)` : "Platform restriction removed" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  if ((tenants.loading && !tenants.data) || (models.loading && !models.data)) return <Card><LoadingState /></Card>;
  const loadError = tenants.error ?? models.error;
  if (loadError && (!tenants.data || !models.data)) return <Card><ErrorState problem={loadError.problem} onRetry={() => { tenants.reload(); models.reload(); }} /></Card>;
  const tenant = tenants.data?.items.find((t) => t.id === tenantId);

  return (
    <Card>
      <CardHeader title="Tenant model allowlists" description="Restrict which models a tenant can use, on top of the tenant's own AI settings." />
      <form onSubmit={save} className="flex flex-col gap-4 px-5 py-4">
        <Notice tone="info">
          The gateway does not expose a tenant&apos;s current allowlist to the platform console, so this form starts empty. Saving replaces the whole list; save an empty list to remove the restriction.
        </Notice>
        <Field id="allow-tenant" label="Tenant">
          {(p) => (
            <Select {...p} value={tenantId} onChange={(e) => setTenantId(e.target.value)} className="max-w-md">
              <option value="">Choose a tenant…</option>
              {(tenants.data?.items ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.slug})
                </option>
              ))}
            </Select>
          )}
        </Field>
        {tenantId ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">Allowed models for {tenant?.name ?? "this tenant"}</legend>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {(models.data?.models ?? []).map((m) => (
                <Checkbox
                  key={m.model_id}
                  label={m.display_name}
                  description={<span className="font-mono">{m.model_id}{m.enabled ? "" : " · disabled"}</span>}
                  checked={selected.includes(m.model_id)}
                  disabled={!canManage || busy}
                  onChange={(e) => setSelected((s) => (e.target.checked ? [...s, m.model_id] : s.filter((x) => x !== m.model_id)))}
                />
              ))}
            </div>
            <p className="text-xs text-muted">{selected.length === 0 ? "No models selected: the platform will not restrict this tenant." : `${selected.length} selected (at most 20).`}</p>
          </fieldset>
        ) : null}
        <FormError message={error && !error.isStepUp ? error.message : null} />
        {canManage && tenantId ? (
          <div className="flex items-center justify-end gap-3 border-t border-border pt-4">
            <StepUpHint />
            <Button type="submit" disabled={busy || selected.length > 20}>
              {busy ? "Saving…" : "Save allowlist"}
            </Button>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

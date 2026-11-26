"use client";
/**
 * Platform AI gateway: registered models (enable/disable, prices, allowed data classes) and per-purpose
 * routing (ordered preference, optional failover). Changes need `platform:ai:manage` and a recent step-up,
 * and are audited by the gateway.
 */
import { ArrowDown, ArrowUp, Pencil, Plus, X } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, ErrorState, Field, FormError, Input, LoadingState, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { formatUsd } from "@/components/analytics/lib/format";
import { StepUpHint } from "./common";
import { type AiModel, CLASSIFICATIONS, microsToUsdInput, modelPatch, moveItem, type Route, validateRoute } from "./lib/ai";

type ModelsOut = { models: AiModel[]; routing: Route[] };

export function AiModels() {
  const can = useCan();
  const data = useApi<ModelsOut>("/api/v1/platform/ai/models");
  const [editing, setEditing] = React.useState<AiModel | null>(null);
  const canManage = can("platform:ai:manage");

  if (data.loading && !data.data) return <Card><LoadingState label="Loading models" /></Card>;
  if (data.error && !data.data) return <Card><ErrorState problem={data.error.problem} onRetry={data.reload} /></Card>;
  const models = data.data?.models ?? [];
  const routes = data.data?.routing ?? [];

  const columns: Column<AiModel>[] = [
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
    { key: "state", header: "State", cell: (m) => <Badge tone={m.enabled ? "ok" : "neutral"}>{m.enabled ? "Enabled" : "Disabled"}</Badge> },
    {
      key: "price",
      header: "Price / 1M tokens",
      cell: (m) => (
        <span className="tabular text-xs">
          in {formatUsd(m.input_price_micros_per_mtok)} · out {formatUsd(m.output_price_micros_per_mtok)}
        </span>
      ),
    },
    { key: "classes", header: "Data classes", hideOnMobile: true, cell: (m) => <span className="text-xs">{m.allowed_classifications.join(", ")}</span> },
    { key: "purposes", header: "Purposes", hideOnMobile: true, cell: (m) => <span className="text-xs">{m.purposes.join(", ").replace(/_/g, " ") || "—"}</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (m) =>
        canManage ? (
          <Button size="sm" variant="ghost" onClick={() => setEditing(m)} aria-label={`Edit ${m.model_id}`}>
            <Pencil className="h-4 w-4" aria-hidden /> Edit
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader title="Providers and models" description="Only enabled models can be routed to or allowed for tenants. Prices drive cost attribution and budgets." />
        <DataTable caption="AI models" columns={columns} rows={models} rowKey={(m) => m.model_id} empty={<EmptyState title="No models are registered" description="Models ship through migrations." />} />
      </Card>
      <Card>
        <CardHeader title="Routing" description="For each purpose, the gateway uses the first available model in order. Failover tries the next one when a provider fails." />
        {routes.length === 0 ? (
          <EmptyState title="No routing policies" />
        ) : (
          <ul className="divide-y divide-border">
            {routes.map((r) => (
              <RouteRow key={`${r.purpose}:${r.updated_at}`} route={r} models={models} canManage={canManage} onSaved={data.reload} />
            ))}
          </ul>
        )}
      </Card>
      {editing ? <ModelDialog model={editing} onClose={() => setEditing(null)} onSaved={data.reload} /> : null}
    </div>
  );
}

function ModelDialog({ model, onClose, onSaved }: { model: AiModel; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [enabled, setEnabled] = React.useState(model.enabled);
  const [input, setInput] = React.useState(microsToUsdInput(model.input_price_micros_per_mtok));
  const [output, setOutput] = React.useState(microsToUsdInput(model.output_price_micros_per_mtok));
  const [classes, setClasses] = React.useState<string[]>(model.allowed_classifications);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const { patch, errors: local } = modelPatch(model, { enabled, input, output, classifications: classes });
    setErrors(local);
    if (Object.keys(local).length) return;
    if (!patch) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/models/${encodeURIComponent(model.model_id)}`, { method: "PATCH", body: patch });
      toast({ tone: "success", title: `${model.display_name} updated` });
      onSaved();
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title={`Edit ${model.display_name}`} description={model.model_id} size="md">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        <Checkbox label="Enabled" description="Disabling stops new requests to this model immediately." checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={busy} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="price-in" label="Input price" hint="USD per 1M tokens" error={errors.input_price_micros_per_mtok}>
            {(p) => <Input {...p} inputMode="decimal" value={input} onChange={(e) => setInput(e.target.value)} disabled={busy} />}
          </Field>
          <Field id="price-out" label="Output price" hint="USD per 1M tokens" error={errors.output_price_micros_per_mtok}>
            {(p) => <Input {...p} inputMode="decimal" value={output} onChange={(e) => setOutput(e.target.value)} disabled={busy} />}
          </Field>
        </div>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Data classifications allowed</legend>
          <div className="grid grid-cols-2 gap-2">
            {CLASSIFICATIONS.map((c) => (
              <Checkbox
                key={c}
                label={c}
                checked={classes.includes(c)}
                disabled={busy}
                onChange={(e) => setClasses((s) => (e.target.checked ? [...s, c] : s.filter((x) => x !== c)))}
              />
            ))}
          </div>
          {errors.allowed_classifications ? <p className="text-xs text-danger">{errors.allowed_classifications}</p> : null}
        </fieldset>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RouteRow({ route, models, canManage, onSaved }: { route: Route; models: AiModel[]; canManage: boolean; onSaved: () => void }) {
  const toast = useToast();
  const [list, setList] = React.useState(route.models);
  const [failover, setFailover] = React.useState(route.failover_allowed);
  const [adding, setAdding] = React.useState("");
  const [localError, setLocalError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const registered = React.useMemo(() => new Set(models.map((m) => m.model_id)), [models]);
  const byId = React.useMemo(() => new Map(models.map((m) => [m.model_id, m])), [models]);
  const dirty = list.join() !== route.models.join() || failover !== route.failover_allowed;
  const candidates = models.filter((m) => !list.includes(m.model_id));

  async function save() {
    const err = validateRoute(list, registered);
    setLocalError(err);
    if (err) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/routing/${encodeURIComponent(route.purpose)}`, { method: "PUT", body: { models: list, failover_allowed: failover } });
      toast({ tone: "success", title: `Routing for ${route.purpose.replace(/_/g, " ")} saved` });
      onSaved();
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-3 px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium">{route.purpose.replace(/_/g, " ")}</p>
          <p className="text-xs text-muted">Updated {formatDateTime(route.updated_at)}</p>
        </div>
        <Badge tone={failover ? "brand" : "neutral"}>{failover ? "Failover on" : "No failover"}</Badge>
      </div>
      <ol className="flex flex-col gap-1.5" aria-label={`Model order for ${route.purpose}`}>
        {list.map((id, i) => {
          const m = byId.get(id);
          return (
            <li key={id} className="flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm">
              <span className="tabular w-5 text-muted">{i + 1}.</span>
              <span className="min-w-0 flex-1 truncate">
                <span className="font-mono text-xs">{id}</span>
                {m && !m.enabled ? <span className="ml-2"><Badge tone="warn">disabled</Badge></span> : null}
              </span>
              {canManage ? (
                <span className="flex gap-1">
                  <Button type="button" size="sm" variant="ghost" aria-label={`Move ${id} up`} disabled={i === 0 || busy} onClick={() => setList((l) => moveItem(l, i, -1))}>
                    <ArrowUp className="h-4 w-4" aria-hidden />
                  </Button>
                  <Button type="button" size="sm" variant="ghost" aria-label={`Move ${id} down`} disabled={i === list.length - 1 || busy} onClick={() => setList((l) => moveItem(l, i, 1))}>
                    <ArrowDown className="h-4 w-4" aria-hidden />
                  </Button>
                  <Button type="button" size="sm" variant="ghost" aria-label={`Remove ${id}`} disabled={busy} onClick={() => setList((l) => l.filter((x) => x !== id))}>
                    <X className="h-4 w-4" aria-hidden />
                  </Button>
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      {canManage ? (
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="sr-only">Add a model to {route.purpose}</span>
            <Select value={adding} onChange={(e) => setAdding(e.target.value)} className="w-56" disabled={busy || list.length >= 5}>
              <option value="">Add a model…</option>
              {candidates.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.model_id}
                </option>
              ))}
            </Select>
          </label>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            disabled={!adding || busy}
            onClick={() => {
              setList((l) => [...l, adding]);
              setAdding("");
            }}
          >
            <Plus className="h-4 w-4" aria-hidden /> Add
          </Button>
          <Checkbox label="Allow failover" checked={failover} onChange={(e) => setFailover(e.target.checked)} disabled={busy} />
          <span className="ml-auto flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="secondary"
              disabled={!dirty || busy}
              onClick={() => {
                setList(route.models);
                setFailover(route.failover_allowed);
                setLocalError(null);
              }}
            >
              Discard
            </Button>
            <Button type="button" size="sm" disabled={!dirty || busy} onClick={save}>
              {busy ? "Saving…" : "Save route"}
            </Button>
          </span>
        </div>
      ) : null}
      {localError ? <p className="text-xs text-danger">{localError}</p> : null}
      <FormError message={error && !error.isStepUp ? error.message : null} />
    </li>
  );
}

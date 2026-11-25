"use client";
/** Feature flags with environment targeting and tenant overridability (build.md §18.2). */
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { relativeTime } from "@/lib/format";
import type { FeatureFlag, Page } from "@/lib/types";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, Field, FormError, Input, StatusBadge, useToast } from "@/components/ui";

const ENVS = ["development", "test", "staging", "production"] as const;
type Draft = { key: string; description: string; enabled: boolean; tenant_overridable: boolean; environments: string[] };

export function FeatureFlags({ readOnly }: { readOnly: boolean }) {
  const toast = useToast();
  const flags = useApi<Page<FeatureFlag>>("/api/v1/platform/feature-flags");
  const [editing, setEditing] = React.useState<{ draft: Draft; isNew: boolean } | null>(null);

  const columns: Column<FeatureFlag>[] = [
    {
      key: "key",
      header: "Flag",
      cell: (f) => (
        <div>
          <p className="font-mono text-xs font-medium">{f.key}</p>
          {f.description ? <p className="text-xs text-muted">{f.description}</p> : null}
        </div>
      ),
    },
    { key: "enabled", header: "State", cell: (f) => <StatusBadge status={f.enabled ? "active" : "disabled"} /> },
    { key: "envs", header: "Environments", cell: (f) => (f.environments.length ? <div className="flex flex-wrap gap-1">{f.environments.map((e) => <Badge key={e}>{e}</Badge>)}</div> : <span className="text-muted">all</span>) },
    { key: "override", header: "Tenant override", cell: (f) => (f.tenant_overridable ? "Allowed" : "Locked"), hideOnMobile: true },
    { key: "updated", header: "Updated", cell: (f) => relativeTime(f.updated_at), hideOnMobile: true },
    {
      key: "edit",
      header: <span className="sr-only">Edit</span>,
      className: "text-right",
      cell: (f) =>
        readOnly ? null : (
          <Button size="sm" variant="secondary" onClick={() => setEditing({ isNew: false, draft: { key: f.key, description: f.description, enabled: f.enabled, tenant_overridable: f.tenant_overridable, environments: f.environments } })}>
            Edit
          </Button>
        ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Feature flags"
        description="Environment targeting applies to every tenant; tenants may override only flags marked overridable."
        actions={
          readOnly ? null : (
            <Button size="sm" onClick={() => setEditing({ isNew: true, draft: { key: "", description: "", enabled: false, tenant_overridable: true, environments: [] } })}>
              New flag
            </Button>
          )
        }
      />
      <DataTable caption="Feature flags" columns={columns} rows={flags.data?.items ?? []} rowKey={(f) => f.key} loading={flags.loading} error={flags.error} onRetry={flags.reload} empty={<EmptyState title="No feature flags" />} />
      <FlagDialog
        state={editing}
        onClose={() => setEditing(null)}
        onSaved={(key) => {
          setEditing(null);
          flags.reload();
          toast({ tone: "success", title: `Flag ${key} saved` });
        }}
      />
    </Card>
  );
}

function FlagDialog({ state, onClose, onSaved }: { state: { draft: Draft; isNew: boolean } | null; onClose: () => void; onSaved: (key: string) => void }) {
  const [draft, setDraft] = React.useState<Draft | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    setDraft(state ? { ...state.draft } : null);
    setError(null);
  }, [state]);
  if (!state || !draft) return <Dialog open={false} onClose={onClose} title="Feature flag" />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const { key, ...body } = draft;
      await api(`/api/v1/platform/feature-flags/${encodeURIComponent(key)}`, { method: "PUT", body });
      onSaved(key);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title={state.isNew ? "New feature flag" : `Edit ${draft.key}`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="flag-key" label="Key" required hint="Lowercase letters, digits, dots and underscores." error={error?.fields.key}>
          {(p) => <Input {...p} className="font-mono" value={draft.key} disabled={!state.isNew} pattern="[a-z][a-z0-9_.]{2,63}" onChange={(e) => setDraft({ ...draft, key: e.target.value.toLowerCase() })} />}
        </Field>
        <Field id="flag-desc" label="Description" error={error?.fields.description}>
          {(p) => <Input {...p} value={draft.description} maxLength={300} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />}
        </Field>
        <Checkbox label="Enabled" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        <Checkbox label="Tenants may override" checked={draft.tenant_overridable} onChange={(e) => setDraft({ ...draft, tenant_overridable: e.target.checked })} />
        <fieldset>
          <legend className="mb-1 text-sm font-medium">Environments (none selected = all)</legend>
          <div className="flex flex-wrap gap-4">
            {ENVS.map((env) => (
              <Checkbox
                key={env}
                label={env}
                checked={draft.environments.includes(env)}
                onChange={(e) => setDraft({ ...draft, environments: e.target.checked ? [...draft.environments, env] : draft.environments.filter((x) => x !== env) })}
              />
            ))}
          </div>
        </fieldset>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !/^[a-z][a-z0-9_.]{2,63}$/.test(draft.key)}>
            {busy ? "Saving…" : "Save flag"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

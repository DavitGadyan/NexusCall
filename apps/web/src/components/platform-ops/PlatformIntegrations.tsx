"use client";
/**
 * Platform-level provider connections (the platform's own Twilio, SES, model and OAuth-app credentials,
 * owned by one service each). Credentials are write-only; create, rotate and revoke need
 * `platform:integrations:manage` and a recent step-up and are audited.
 */
import { Plus } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, Dialog, EmptyState, Field, FormError, Input, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { ConfigFields, CredentialFields } from "@/components/integrations/CredentialFields";
import { ConnectionPanel } from "@/components/integrations/ConnectionDetail";
import {
  buildConfig,
  buildCredentials,
  credentialFieldErrors,
  FALLBACK_PROVIDERS,
  healthInfo,
  platformProviders,
  providerLabel,
  rotationStatus,
} from "@/components/integrations/lib/connections";
import type { Connection, Provider } from "@/components/integrations/lib/types";
import { StepUpHint } from "./common";

function useProviders(): Provider[] {
  // The provider catalogue endpoint is tenant-scoped; without a workspace fall back to the mirrored list.
  const remote = useApi<{ items: Provider[] }>("/api/v1/integrations/providers");
  const items = remote.data?.items?.length ? remote.data.items : FALLBACK_PROVIDERS;
  return platformProviders(items);
}

export function PlatformIntegrations() {
  const can = useCan();
  const list = useCursorList<Connection>("/api/v1/platform/integrations/connections");
  const providers = useProviders();
  const [selected, setSelected] = React.useState<Connection | null>(null);
  const [creating, setCreating] = React.useState(false);
  const canManage = can("platform:integrations:manage");
  const now = Date.now();

  const columns: Column<Connection>[] = [
    {
      key: "provider",
      header: "Provider",
      cell: (c) => (
        <button type="button" onClick={() => setSelected(c)} className="text-left text-brand hover:underline" aria-expanded={selected?.id === c.id}>
          <span className="font-medium">{c.display_name || providerLabel(c.provider)}</span>
          <span className="block text-xs text-muted">{providerLabel(c.provider)}</span>
        </button>
      ),
    },
    { key: "owner", header: "Owning service", cell: (c) => <span className="font-mono text-xs">{c.owning_service}</span> },
    { key: "account", header: "Account", hideOnMobile: true, cell: (c) => <span className="font-mono text-xs">{c.account_masked ?? "—"}</span> },
    {
      key: "health",
      header: "Health",
      cell: (c) => {
        const h = healthInfo(c.health);
        return (
          <span title={h.help}>
            <Badge tone={h.tone}>{h.label}</Badge>
          </span>
        );
      },
    },
    { key: "validated", header: "Last validated", hideOnMobile: true, cell: (c) => <span title={formatDateTime(c.last_validated_at)}>{relativeTime(c.last_validated_at, now)}</span> },
    {
      key: "rotation",
      header: "Rotation due",
      hideOnMobile: true,
      cell: (c) => {
        if (c.status === "revoked") return "—";
        const r = rotationStatus(c.next_rotation_due, now);
        return <Badge tone={r.tone}>{r.label}</Badge>;
      },
    },
  ];

  const spec = selected ? (providers.find((p) => p.provider === selected.provider) ?? FALLBACK_PROVIDERS.find((p) => p.provider === selected.provider) ?? null) : null;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader
          title="Platform connections"
          description="Default credentials used by platform services when a tenant has not connected its own account."
          actions={
            canManage ? (
              <Button onClick={() => setCreating(true)} data-testid="new-platform-connection">
                <Plus className="h-4 w-4" aria-hidden /> Add connection
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Platform connections"
          columns={columns}
          rows={list.items}
          rowKey={(c) => c.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No platform connections" description="Services use the development fakes until a platform credential is connected." />}
        />
      </Card>
      {selected ? (
        <ConnectionPanel
          key={selected.id}
          connection={selected}
          spec={spec}
          base="/api/v1/platform/integrations/connections"
          managePermission="platform:integrations:manage"
          allowEnable={false}
          onChange={(c) => {
            setSelected(c);
            list.reload();
          }}
        />
      ) : null}
      {creating ? (
        <CreatePlatformConnection
          providers={providers}
          onClose={() => setCreating(false)}
          onCreated={(c) => {
            list.reload();
            setSelected(c);
          }}
        />
      ) : null}
    </div>
  );
}

function CreatePlatformConnection({ providers, onClose, onCreated }: { providers: Provider[]; onClose: () => void; onCreated: (c: Connection) => void }) {
  const toast = useToast();
  const [providerKey, setProviderKey] = React.useState(providers[0]?.provider ?? "");
  const spec = providers.find((p) => p.provider === providerKey) ?? null;
  const [owner, setOwner] = React.useState(spec?.platform_services[0] ?? "");
  const [displayName, setDisplayName] = React.useState("");
  const [creds, setCreds] = React.useState<Record<string, string>>({});
  const [config, setConfig] = React.useState<Record<string, string>>({});
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setOwner(spec?.platform_services[0] ?? "");
    setCreds({});
    setConfig({});
    setErrors({});
  }, [spec]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!spec) return;
    const built = buildCredentials(spec, creds);
    const local: Record<string, string> = { ...built.errors };
    if (!spec.platform_services.includes(owner)) local.owning_service = "Choose the service that owns this credential.";
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const c = await api<Connection>("/api/v1/platform/integrations/connections", {
        method: "POST",
        body: { provider: spec.provider, owning_service: owner, display_name: displayName.trim() || null, credentials: built.credentials, config: buildConfig(spec, config) },
      });
      setCreds({});
      toast({ tone: c.last_error ? "error" : "success", title: c.last_error ? "Saved, but the test failed" : "Platform connection added", description: c.last_error ?? undefined });
      onCreated(c);
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(credentialFieldErrors(x.problem, x.fields));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title="Add platform connection" description="Write-only: after saving, the values cannot be viewed, only rotated or revoked." size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="off" noValidate>
        <StepUpHint />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="pc-provider" label="Provider">
            {(p) => (
              <Select {...p} value={providerKey} onChange={(e) => setProviderKey(e.target.value)} disabled={busy}>
                {providers.map((pr) => (
                  <option key={pr.provider} value={pr.provider}>
                    {providerLabel(pr.provider)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="pc-owner" label="Owning service" hint="Only this service can read the credential." error={errors.owning_service}>
            {(p) => (
              <Select {...p} value={owner} onChange={(e) => setOwner(e.target.value)} disabled={busy}>
                {(spec?.platform_services ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="pc-name" label="Display name" hint="Optional." error={errors.display_name}>
            {(p) => <Input {...p} value={displayName} maxLength={120} onChange={(e) => setDisplayName(e.target.value)} disabled={busy} />}
          </Field>
        </div>
        {spec ? (
          <>
            <CredentialFields spec={spec} values={creds} errors={errors} disabled={busy} idPrefix="pc" onChange={(n, v) => setCreds((c) => ({ ...c, [n]: v }))} />
            <ConfigFields keys={spec.config_keys} values={config} errors={errors} disabled={busy} idPrefix="pc" onChange={(n, v) => setConfig((c) => ({ ...c, [n]: v }))} />
          </>
        ) : null}
        <FormError message={error && !error.isStepUp ? (Object.keys(errors).length ? "Some fields need attention." : error.message) : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !spec}>
            {busy ? "Saving and testing…" : "Save and test"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

"use client";
/**
 * Secrets health: vault references with owner, environment, rotation and retrieval status. Metadata only;
 * there is no way to read a secret value here. The health check reads vault metadata (never secret data)
 * and is audited.
 */
import { RefreshCw } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, EmptyState, FormError, Input, Notice, Select, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { PROVIDER_LABELS, providerLabel, rotationStatus } from "@/components/integrations/lib/connections";
import type { SecretRef } from "@/components/integrations/lib/types";
import { secretHealth, shortRef, summarizeSecrets } from "./lib/secrets";

export function SecretsHealth() {
  const can = useCan();
  const toast = useToast();
  const [scope, setScope] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [provider, setProvider] = React.useState("");
  const [tenant, setTenant] = React.useState("");
  const tenantOk = tenant === "" || /^[0-9a-f-]{36}$/i.test(tenant.trim());
  const list = useCursorList<SecretRef>(
    withQuery("/api/v1/platform/secrets", { scope, status, provider, tenant_id: tenantOk ? tenant.trim() : undefined }),
  );
  const [checking, setChecking] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const sum = summarizeSecrets(list.items);
  const now = Date.now();

  async function check() {
    setChecking(true);
    setError(null);
    try {
      const out = await api<{ checked: number }>("/api/v1/platform/secrets/health-check", { method: "POST" });
      toast({ tone: "success", title: `Checked ${out.checked} vault paths` });
      list.reload();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setChecking(false);
    }
  }

  const columns: Column<SecretRef>[] = [
    {
      key: "ref",
      header: "Reference",
      cell: (s) => (
        <span>
          <span className="font-mono text-xs" title={s.vault_ref}>
            {shortRef(s.vault_ref)}
          </span>
          <span className="block text-xs text-muted">
            {s.kind.replace(/_/g, " ")}
            {s.vault_version ? ` · v${s.vault_version}` : ""}
          </span>
        </span>
      ),
    },
    {
      key: "owner",
      header: "Owner",
      cell: (s) => (
        <span className="text-xs">
          {providerLabel(s.provider)} · <span className="font-mono">{s.owning_service}</span>
          <span className="block text-muted">{s.scope === "tenant" ? `tenant ${shortId(s.tenant_id)}` : "platform"} · {s.environment}</span>
        </span>
      ),
    },
    {
      key: "health",
      header: "Health",
      cell: (s) => {
        const h = secretHealth(s.health);
        return (
          <span className="flex flex-col gap-0.5">
            <Badge tone={h.tone}>{h.label}</Badge>
            {s.last_vault_status && s.last_vault_status !== "ok" ? <span className="text-xs text-danger">vault: {s.last_vault_status.replace(/_/g, " ")}</span> : null}
          </span>
        );
      },
    },
    {
      key: "rotation",
      header: "Rotation",
      hideOnMobile: true,
      cell: (s) => {
        const r = rotationStatus(s.next_rotation_due, now);
        return (
          <span className="text-xs">
            <Badge tone={r.tone}>{r.label}</Badge>
            <span className="block text-muted">last {relativeTime(s.last_rotated_at, now)}</span>
          </span>
        );
      },
    },
    { key: "retrieved", header: "Last retrieved", hideOnMobile: true, cell: (s) => <span title={formatDateTime(s.last_retrieval_at)}>{relativeTime(s.last_retrieval_at, now)}</span> },
    { key: "checked", header: "Last checked", hideOnMobile: true, cell: (s) => <span title={formatDateTime(s.last_checked_at)}>{relativeTime(s.last_checked_at, now)}</span> },
  ];

  return (
    <div className="flex flex-col gap-4">
      <Notice tone="info" title="Metadata only">
        This view lists where secrets live and how fresh they are. Secret values are never returned by the API and cannot be revealed here.
      </Notice>
      {list.items.length > 0 && sum.attention > 0 ? (
        <Notice tone="warn" title={`${sum.attention} of the loaded references need attention`}>
          Rotate overdue credentials from the owning connection; vault errors are retried by the health check.
        </Notice>
      ) : null}
      <Card>
        <CardHeader
          title="Secret references"
          description="Tenant and platform credentials registered by the integration service."
          actions={
            can("platform:secrets:manage") ? (
              <Button variant="secondary" onClick={check} disabled={checking} data-testid="secrets-health-check">
                <RefreshCw className="h-4 w-4" aria-hidden /> {checking ? "Checking…" : "Run health check"}
              </Button>
            ) : null
          }
        />
        <div className="flex flex-wrap gap-3 border-b border-border px-5 py-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Scope</span>
            <Select value={scope} onChange={(e) => setScope(e.target.value)} className="w-32">
              <option value="">All</option>
              <option value="platform">Platform</option>
              <option value="tenant">Tenant</option>
            </Select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Status</span>
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-36">
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="error">Error</option>
              <option value="disabled">Disabled</option>
              <option value="revoked">Revoked</option>
            </Select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Provider</span>
            <Select value={provider} onChange={(e) => setProvider(e.target.value)} className="w-48">
              <option value="">All</option>
              {Object.keys(PROVIDER_LABELS).map((p) => (
                <option key={p} value={p}>
                  {providerLabel(p)}
                </option>
              ))}
            </Select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Tenant ID</span>
            <Input value={tenant} onChange={(e) => setTenant(e.target.value)} className="w-80 font-mono text-xs" aria-invalid={!tenantOk || undefined} placeholder="uuid" />
          </label>
        </div>
        <div className="px-5">
          <FormError message={error && !error.isStepUp ? error.message : null} />
        </div>
        <DataTable
          caption="Secret references"
          columns={columns}
          rows={list.items}
          rowKey={(s) => s.vault_ref}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No secret references match" />}
        />
      </Card>
    </div>
  );
}

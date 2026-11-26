"use client";
/**
 * One provider connection. Test, rotate, disable/enable and revoke; rotation and revocation need a recent
 * step-up and are audited, revocation also a typed confirmation. The stored secret is never shown: the page
 * displays its vault reference, version and rotation metadata only.
 */
import { KeyRound, Power, RefreshCw, ShieldAlert, Stethoscope } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  KeyValues,
  LoadingState,
  Notice,
  Select,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { Mono, StepUpHint } from "@/components/platform-ops/common";
import { CredentialFields } from "./CredentialFields";
import {
  buildCredentials,
  connectionActions,
  credentialFieldErrors,
  healthInfo,
  providerLabel,
  revokePhrase,
  rotationStatus,
  safeProviderUrl,
} from "./lib/connections";
import type { AuditEvent, Connection, Provider, RotateReason } from "./lib/types";

/**
 * `scope: "tenant"` uses `/api/v1/integrations/connections`; `scope: "platform"` the platform console's
 * `/api/v1/platform/integrations/connections` (no single-item GET there, so the row is passed in).
 */
export function ConnectionDetail({ connectionId, listHref }: { connectionId: string; listHref: string }) {
  const conn = useApi<Connection>(`/api/v1/integrations/connections/${connectionId}`);
  const providers = useApi<{ items: Provider[] }>("/api/v1/integrations/providers");
  const spec = providers.data?.items.find((p) => p.provider === conn.data?.provider) ?? null;

  if (conn.loading && !conn.data) return <Card><LoadingState label="Loading connection" /></Card>;
  if (conn.error && !conn.data) return <Card><ErrorState problem={conn.error.problem} onRetry={conn.reload} /></Card>;
  if (!conn.data) return null;
  return (
    <div className="flex flex-col gap-4">
      <Link href={listHref} className="text-sm text-brand hover:underline">
        ← All connections
      </Link>
      <ConnectionPanel
        connection={conn.data}
        spec={spec}
        base="/api/v1/integrations/connections"
        managePermission="integrations:manage"
        onChange={(c) => conn.setData(() => c)}
        canReconnectOAuth
      />
      <ConnectionActivity connectionId={connectionId} />
    </div>
  );
}

export function ConnectionPanel({
  connection: c,
  spec,
  base,
  managePermission,
  onChange,
  canReconnectOAuth = false,
  allowEnable = true,
}: {
  connection: Connection;
  spec: Provider | null;
  base: string;
  managePermission: string;
  onChange: (c: Connection) => void;
  canReconnectOAuth?: boolean;
  allowEnable?: boolean;
}) {
  const can = useCan();
  const toast = useToast();
  const canManage = can(managePermission);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [rotating, setRotating] = React.useState(false);
  const [revoking, setRevoking] = React.useState(false);
  const actions = connectionActions(c);
  const h = healthInfo(c.health);
  const rot = rotationStatus(c.next_rotation_due);

  async function run(name: string, path: string, success: string) {
    setBusy(name);
    setError(null);
    try {
      const next = await api<Connection>(`${base}/${c.id}/${path}`, { method: "POST" });
      onChange(next);
      if (name === "test") {
        const nh = healthInfo(next.health);
        toast({ tone: next.last_error ? "error" : "success", title: next.last_error ? "Test failed" : "Test passed", description: next.last_error ?? nh.label });
      } else toast({ tone: "success", title: success });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(null);
    }
  }

  async function reconnect() {
    setBusy("oauth");
    setError(null);
    try {
      const out = await api<{ authorization_url: string }>(`/api/v1/integrations/oauth/${encodeURIComponent(c.provider)}/start`);
      const url = safeProviderUrl(out.authorization_url);
      if (!url) {
        toast({ tone: "error", title: "The provider sign-in link was rejected", description: "Only secure https links are followed." });
        return;
      }
      window.location.assign(url);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <Card>
        <CardHeader
          title={c.display_name || providerLabel(c.provider)}
          description={`${providerLabel(c.provider)}${c.scope && c.scope !== "default" ? ` · ${c.scope}` : ""} · ${c.auth_type === "oauth" ? "OAuth" : "Static credentials"}`}
          actions={
            <span title={h.help}>
              <Badge tone={h.tone}>{h.label}</Badge>
            </span>
          }
        />
        <div className="flex flex-col gap-4 px-5 py-4">
          {c.last_error ? <Notice tone="danger" title="Last error">{c.last_error}</Notice> : null}
          {c.status === "revoked" ? <Notice tone="info">This connection is revoked. Its stored credentials were deleted; create a new connection to reconnect.</Notice> : null}
          <FormError message={error && !error.isStepUp ? error.message : null} />
          <KeyValues
            items={[
              { label: "Status", value: c.status },
              { label: "Account", value: <Mono>{c.account_masked ?? "—"}</Mono> },
              { label: "Owning service", value: c.owning_service },
              { label: "Last validated", value: formatDateTime(c.last_validated_at) },
              { label: "Last used", value: formatDateTime(c.last_resolved_at) },
              { label: "Last rotated", value: formatDateTime(c.last_rotated_at) },
              {
                label: "Rotation due",
                value: c.status === "revoked" ? "—" : (
                  <span className="flex flex-wrap items-center gap-2">
                    {formatDateTime(c.next_rotation_due)} <Badge tone={rot.tone}>{rot.label}</Badge>
                  </span>
                ),
              },
              { label: "Secret reference", value: <Mono>{c.vault_ref}{c.vault_version ? ` (v${c.vault_version})` : ""}</Mono> },
              { label: "Vault status", value: c.last_vault_status ?? "—" },
              ...(c.granted_scopes.length ? [{ label: "Granted scopes", value: <Mono>{c.granted_scopes.join(" ")}</Mono> }] : []),
              ...(Object.keys(c.config ?? {}).length
                ? [{ label: "Settings", value: <Mono>{Object.entries(c.config).map(([k, v]) => `${k}=${v}`).join(", ")}</Mono> }]
                : []),
            ]}
          />
        </div>
        {canManage && c.status !== "revoked" ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-5 py-3">
            {actions.test ? (
              <Button variant="secondary" size="sm" onClick={() => run("test", "test", "Tested")} disabled={busy !== null} data-testid="test-connection">
                <Stethoscope className="h-4 w-4" aria-hidden /> {busy === "test" ? "Testing…" : "Test"}
              </Button>
            ) : null}
            {actions.rotate ? (
              <Button variant="secondary" size="sm" onClick={() => setRotating(true)} disabled={busy !== null || !spec} data-testid="rotate-connection">
                <KeyRound className="h-4 w-4" aria-hidden /> Rotate
              </Button>
            ) : null}
            {canReconnectOAuth && c.auth_type === "oauth" && c.status !== "disabled" ? (
              <Button variant="secondary" size="sm" onClick={reconnect} disabled={busy !== null}>
                <RefreshCw className="h-4 w-4" aria-hidden /> {busy === "oauth" ? "Opening…" : "Reconnect"}
              </Button>
            ) : null}
            {actions.disable ? (
              <Button variant="secondary" size="sm" onClick={() => run("disable", "disable", "Connection disabled")} disabled={busy !== null}>
                <Power className="h-4 w-4" aria-hidden /> {busy === "disable" ? "Disabling…" : "Disable"}
              </Button>
            ) : null}
            {actions.enable && allowEnable ? (
              <Button variant="secondary" size="sm" onClick={() => run("enable", "enable", "Connection enabled")} disabled={busy !== null}>
                <Power className="h-4 w-4" aria-hidden /> {busy === "enable" ? "Enabling…" : "Enable"}
              </Button>
            ) : null}
            {actions.revoke ? (
              <Button variant="danger" size="sm" onClick={() => setRevoking(true)} disabled={busy !== null} className="ml-auto" data-testid="revoke-connection">
                <ShieldAlert className="h-4 w-4" aria-hidden /> Revoke
              </Button>
            ) : null}
            <span className="w-full text-xs text-muted">Rotate and revoke need a multi-factor sign-in within the last five minutes and are recorded in the audit log.</span>
          </div>
        ) : null}
      </Card>
      {spec ? (
        <RotateDialog open={rotating} onClose={() => setRotating(false)} connection={c} spec={spec} base={base} onRotated={onChange} />
      ) : null}
      <ConfirmDialog
        open={revoking}
        onClose={() => setRevoking(false)}
        title={`Revoke ${providerLabel(c.provider)}`}
        description="Revoking deletes the stored credentials and stops every feature that uses this connection. It cannot be undone."
        confirmLabel="Revoke connection"
        typedConfirmation={revokePhrase(c)}
        onConfirm={async () => {
          const next = await api<Connection>(`${base}/${c.id}/revoke`, { method: "POST" });
          onChange(next);
          toast({ tone: "success", title: "Connection revoked" });
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </>
  );
}

const REASONS: { value: RotateReason; label: string }[] = [
  { value: "planned", label: "Planned rotation" },
  { value: "suspected_compromise", label: "Suspected compromise" },
  { value: "provider_requested", label: "Requested by the provider" },
  { value: "other", label: "Other" },
];

function RotateDialog({
  open,
  onClose,
  connection,
  spec,
  base,
  onRotated,
}: {
  open: boolean;
  onClose: () => void;
  connection: Connection;
  spec: Provider;
  base: string;
  onRotated: (c: Connection) => void;
}) {
  const toast = useToast();
  const [values, setValues] = React.useState<Record<string, string>>({});
  const [reason, setReason] = React.useState<RotateReason>("planned");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    // Opening or closing always starts from an empty form.
    setValues({});
    setErrors({});
    setError(null);
    setReason("planned");
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const built = buildCredentials(spec, values);
    setErrors(built.errors);
    if (Object.keys(built.errors).length) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api<Connection>(`${base}/${connection.id}/rotate`, { method: "POST", body: { credentials: built.credentials, reason } });
      setValues({});
      onRotated(next);
      toast({ tone: next.last_error ? "error" : "success", title: next.last_error ? "Rotated, but the test failed" : "Credentials rotated", description: next.last_error ?? undefined });
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
    <Dialog open={open} onClose={onClose} busy={busy} title={`Rotate ${providerLabel(connection.provider)} credentials`} description="Enter the new credentials. They are tested before they replace the current ones." size="md">
      <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="off" noValidate>
        <StepUpHint />
        <CredentialFields spec={spec} values={values} errors={errors} disabled={busy} idPrefix="rotate" onChange={(n, v) => setValues((s) => ({ ...s, [n]: v }))} />
        <Field id="rotate-reason" label="Reason" error={errors.reason}>
          {(p) => (
            <Select {...p} value={reason} onChange={(e) => setReason(e.target.value as RotateReason)} disabled={busy}>
              {REASONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <FormError message={error && !error.isStepUp ? (Object.keys(errors).length ? "Some fields need attention." : error.message) : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} data-testid="confirm-rotate">
            {busy ? "Rotating…" : "Rotate"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ConnectionActivity({ connectionId }: { connectionId: string }) {
  const can = useCan();
  const enabled = can("audit:read");
  const list = useCursorList<AuditEvent>(
    enabled ? withQuery("/api/v1/audit/events", { resource_type: "provider_connection", resource_id: connectionId }) : null,
    20,
  );
  if (!enabled) return null;
  const columns: Column<AuditEvent>[] = [
    { key: "action", header: "Action", cell: (e) => <Mono>{e.action.replace(/^integration\.connection\./, "")}</Mono> },
    { key: "outcome", header: "Outcome", cell: (e) => <Badge tone={e.outcome === "success" ? "ok" : e.outcome === "denied" ? "warn" : "danger"}>{e.outcome}</Badge> },
    { key: "actor", header: "Actor", hideOnMobile: true, cell: (e) => <Mono>{e.actor_type}:{e.actor_id.slice(0, 8)}</Mono> },
    { key: "when", header: "When", cell: (e) => <span title={formatDateTime(e.occurred_at)}>{relativeTime(e.occurred_at)}</span> },
  ];
  return (
    <Card>
      <CardHeader title="Activity" description="From the audit log: creation, tests, rotations and state changes." />
      <DataTable
        caption="Connection activity"
        columns={columns}
        rows={list.items}
        rowKey={(e) => e.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No recorded activity yet" />}
      />
    </Card>
  );
}

"use client";
/**
 * User detail (build.md §18.4, §20.2, §22.4): identity, MFA, active sessions, memberships, effective
 * permissions per tenant, deactivate / reactivate, revoke sessions, platform-role requests (dual approval).
 * No password, token or secret is ever shown: the API does not return them.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { EffectivePermissions, PlatformUserDetail } from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  CardHeader,
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
  PageHeader,
  Select,
  StatusBadge,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan, useSession } from "@/components/shell/session-context";

type Action = "deactivate" | "reactivate" | "revoke" | null;

export function UserDetail({ userId, initialTenant }: { userId: string; initialTenant?: string }) {
  const can = useCan();
  const session = useSession();
  const toast = useToast();
  const user = useApi<PlatformUserDetail>(`/api/v1/platform/users/${userId}`);
  const [action, setAction] = React.useState<Action>(null);
  const [requestingRole, setRequestingRole] = React.useState(false);
  const [removingRole, setRemovingRole] = React.useState<string | null>(null);

  if (user.loading && !user.data) return <Card><LoadingState /></Card>;
  if (user.error && !user.data) return <Card><ErrorState problem={user.error.problem} onRetry={user.reload} /></Card>;
  const u = user.data;
  if (!u) return null;
  const self = u.id === session.user.id;
  const manageUsers = can("platform:users:manage") && !self;
  const manageIam = can("platform:iam:manage") && !self;

  return (
    <>
      <PageHeader
        title={u.display_name ?? u.email}
        description={u.email}
        actions={
          manageUsers ? (
            <>
              <Button size="sm" variant="secondary" onClick={() => setAction("revoke")}>
                Revoke sessions
              </Button>
              {u.status === "active" ? (
                <Button size="sm" variant="danger" onClick={() => setAction("deactivate")}>
                  Deactivate
                </Button>
              ) : (
                <Button size="sm" onClick={() => setAction("reactivate")}>
                  Reactivate
                </Button>
              )}
            </>
          ) : self ? (
            <Badge>This is you</Badge>
          ) : null
        }
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Identity & MFA" description="From tenant-service and the identity provider." />
          <div className="px-5 py-4">
            <KeyValues
              items={[
                { label: "Status", value: <StatusBadge status={u.status} /> },
                { label: "User ID", value: <span className="font-mono text-xs">{u.id}</span> },
                { label: "Last seen", value: formatDateTime(u.last_seen_at) },
                ...(u.idp
                  ? [
                      { label: "IdP account", value: <StatusBadge status={u.idp.enabled ? "active" : "disabled"} /> },
                      { label: "Email verified", value: u.idp.email_verified ? "Yes" : "No" },
                      { label: "MFA enrolled", value: <Badge tone={u.idp.mfa_enrolled ? "ok" : "warn"}>{u.idp.mfa_enrolled ? "Authenticator enrolled" : "Not enrolled"}</Badge> },
                      { label: "Realm roles", value: u.idp.realm_roles.filter((r) => r.startsWith("platform_")).join(", ") || "—" },
                    ]
                  : [{ label: "Identity provider", value: <span className="text-warn">Unavailable: IdP details could not be loaded.</span> }]),
              ]}
            />
          </div>
        </Card>
        <Card>
          <CardHeader
            title="Platform roles"
            description="Granting a platform role needs a second administrator's approval."
            actions={
              manageIam ? (
                <Button size="sm" variant="secondary" onClick={() => setRequestingRole(true)}>
                  Request platform role
                </Button>
              ) : null
            }
          />
          <div className="flex flex-wrap gap-2 px-5 py-4">
            {u.platform_roles.length === 0 ? <p className="text-sm text-muted">No platform roles.</p> : null}
            {u.platform_roles.map((r) => (
              <span key={r} className="inline-flex items-center gap-1">
                <Badge tone="brand">{r}</Badge>
                {manageIam ? (
                  <button type="button" className="text-xs text-danger hover:underline" onClick={() => setRemovingRole(r)}>
                    Remove
                  </button>
                ) : null}
              </span>
            ))}
          </div>
        </Card>
      </div>
      <Card className="mt-4">
        <CardHeader title="Active sessions" description="Revoking ends every session and refresh token at the identity provider." />
        <DataTable
          caption="Active sessions"
          rows={u.sessions}
          rowKey={(s) => s.id}
          columns={[
            { key: "ip", header: "IP address", cell: (s) => <span className="font-mono text-xs">{s.ip ?? "—"}</span> },
            { key: "started", header: "Started", cell: (s) => formatDateTime(s.started) },
            { key: "last", header: "Last access", cell: (s) => relativeTime(s.last_access) },
            { key: "clients", header: "Clients", cell: (s) => s.clients.join(", ") || "—", hideOnMobile: true },
          ]}
          empty={<EmptyState title="No active sessions" />}
        />
      </Card>
      <Card className="mt-4">
        <CardHeader title="Tenant memberships" />
        <DataTable
          caption="Tenant memberships"
          rows={u.memberships}
          rowKey={(m) => m.id}
          columns={[
            { key: "tenant", header: "Tenant", cell: (m) => <Link className="text-brand hover:underline" href={`/platform/tenants/${m.id}`}>{m.name}</Link> },
            { key: "tstatus", header: "Tenant status", cell: (m) => <StatusBadge status={m.status} /> },
            { key: "mstatus", header: "Membership", cell: (m) => <StatusBadge status={m.membership} /> },
          ]}
          empty={<EmptyState title="Not a member of any tenant" />}
        />
      </Card>
      {can("platform:iam:read") ? <EffectivePermissionsViewer userId={u.id} memberships={u.memberships} initialTenant={initialTenant} /> : null}

      <ConfirmDialog
        open={action !== null}
        onClose={() => setAction(null)}
        title={action === "deactivate" ? "Deactivate user" : action === "reactivate" ? "Reactivate user" : "Revoke all sessions"}
        description={
          action === "deactivate"
            ? "The account is disabled at the identity provider, every session is ended and all tenant access stops."
            : action === "reactivate"
              ? "The account can sign in again. Tenant access follows their memberships."
              : "Every active session and refresh token of this user ends now. They must sign in again."
        }
        confirmLabel={action === "deactivate" ? "Deactivate" : action === "reactivate" ? "Reactivate" : "Revoke sessions"}
        tone={action === "reactivate" ? "primary" : "danger"}
        typedConfirmation={action === "deactivate" ? u.email : undefined}
        reason={{ minLength: 5 }}
        onConfirm={async (reason) => {
          const path = action === "deactivate" ? "deactivate" : action === "reactivate" ? "reactivate" : "revoke-sessions";
          await api(`/api/v1/platform/users/${u.id}/${path}`, { method: "POST", body: { reason } });
          toast({ tone: "success", title: action === "revoke" ? "Sessions revoked" : `User ${action}d` });
          user.reload();
        }}
      />
      <RequestRoleDialog
        open={requestingRole}
        userId={u.id}
        onClose={() => setRequestingRole(false)}
        onDone={() => {
          setRequestingRole(false);
          toast({ tone: "success", title: "Request submitted", description: "A second platform administrator must approve it." });
        }}
      />
      <ConfirmDialog
        open={removingRole !== null}
        onClose={() => setRemovingRole(null)}
        title="Remove platform role"
        description="The role is removed at the identity provider and the user's sessions end."
        confirmLabel="Remove role"
        typedConfirmation={removingRole ?? undefined}
        onConfirm={async () => {
          if (!removingRole) return;
          await api(`/api/v1/platform/iam/platform-admins/${u.id}/${encodeURIComponent(removingRole)}`, { method: "DELETE" });
          toast({ tone: "success", title: "Platform role removed" });
          user.reload();
        }}
      />
    </>
  );
}

function EffectivePermissionsViewer({
  userId,
  memberships,
  initialTenant,
}: {
  userId: string;
  memberships: PlatformUserDetail["memberships"];
  initialTenant?: string;
}) {
  const [tenant, setTenant] = React.useState(initialTenant && memberships.some((m) => m.id === initialTenant) ? initialTenant : (memberships[0]?.id ?? ""));
  const eff = useApi<EffectivePermissions>(tenant ? withQuery(`/api/v1/platform/users/${userId}/effective-permissions`, { tenant_id: tenant }) : null);
  const grouped = React.useMemo(() => {
    const g = new Map<string, string[]>();
    for (const p of eff.data?.permissions ?? []) {
      const area = p.split(":")[0] ?? p;
      g.set(area, [...(g.get(area) ?? []), p]);
    }
    return [...g.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [eff.data]);

  return (
    <Card className="mt-4">
      <CardHeader title="Effective permissions" description="What this user can do in a tenant, resolved by tenant-service from their roles." />
      {memberships.length === 0 ? (
        <EmptyState title="No tenant to evaluate" />
      ) : (
        <div className="flex flex-col gap-4 px-5 py-4">
          <Field id="eff-tenant" label="Tenant" className="max-w-xs">
            {(p) => (
              <Select {...p} value={tenant} onChange={(e) => setTenant(e.target.value)}>
                {memberships.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {eff.loading ? (
            <LoadingState rows={3} />
          ) : eff.error ? (
            <ErrorState problem={eff.error.problem} onRetry={eff.reload} />
          ) : eff.data ? (
            <div className="flex flex-col gap-3" data-testid="effective-permissions">
              <p className="text-sm">
                Roles: {eff.data.roles.length ? eff.data.roles.map((r) => <Badge key={r} tone="brand">{r}</Badge>) : <span className="text-muted">none (inactive membership)</span>}
              </p>
              {grouped.length === 0 ? <Notice tone="info">No permissions in this tenant.</Notice> : null}
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {grouped.map(([area, perms]) => (
                  <div key={area} className="rounded-md border border-border p-3">
                    <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{area}</p>
                    <ul className="flex flex-col gap-0.5">
                      {perms.map((p) => (
                        <li key={p} className="font-mono text-xs">
                          {p}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </Card>
  );
}

function RequestRoleDialog({ open, userId, onClose, onDone }: { open: boolean; userId: string; onClose: () => void; onDone: () => void }) {
  const [role, setRole] = React.useState("platform_operator");
  const [reason, setReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setRole("platform_operator");
      setReason("");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/platform/iam/platform-admins", { method: "POST", body: { user_id: userId, role_key: role, reason: reason.trim() } });
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Request a platform role" description="Critical change: another platform administrator must approve it within 24 hours.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="rr-role" label="Role" error={error?.fields.role_key}>
          {(p) => (
            <Select {...p} value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="platform_operator">Platform operator</option>
              <option value="platform_owner">Platform owner</option>
            </Select>
          )}
        </Field>
        <Field id="rr-reason" label="Reason" hint="At least 10 characters; recorded in the audit log." error={error?.fields.reason}>
          {(p) => <Textarea {...p} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />}
        </Field>
        <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || reason.trim().length < 10}>
            {busy ? "Submitting…" : "Submit for approval"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

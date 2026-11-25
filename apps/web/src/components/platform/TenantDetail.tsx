"use client";
/** Tenant detail: lifecycle (suspend / reactivate / archive with a reason), members, invitations, support grants. */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { IamRoles, Invitation, TenantDetail as TenantDetailData } from "@/lib/types";
import {
  Button,
  ButtonLink,
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
  Input,
  KeyValues,
  LoadingState,
  PageHeader,
  Select,
  StatusBadge,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { SCOPE_LABEL } from "@/components/admin/SupportAccess";

type Transition = { status: "active" | "suspended" | "archived"; label: string; description: string; tone: "danger" | "primary" };

export function TenantDetail({ tenantId }: { tenantId: string }) {
  const can = useCan();
  const toast = useToast();
  const { data, error, loading, reload } = useApi<TenantDetailData>(`/api/v1/platform/tenants/${tenantId}`);
  const [transition, setTransition] = React.useState<Transition | null>(null);
  const [inviting, setInviting] = React.useState(false);

  if (loading && !data) return <Card><LoadingState /></Card>;
  if (error && !data) return <Card><ErrorState problem={error.problem} onRetry={reload} /></Card>;
  if (!data) return null;

  const manage = can("platform:tenants:manage");
  const transitions: Transition[] =
    data.status === "active"
      ? [
          { status: "suspended", label: "Suspend", tone: "danger", description: "Members lose access and all calls, emails and AI stop for this tenant. Data is kept." },
          { status: "archived", label: "Archive", tone: "danger", description: "Archiving is final in the UI: the tenant cannot be reactivated here." },
        ]
      : data.status === "suspended"
        ? [
            { status: "active", label: "Reactivate", tone: "primary", description: "Members regain access according to their roles." },
            { status: "archived", label: "Archive", tone: "danger", description: "Archiving is final in the UI: the tenant cannot be reactivated here." },
          ]
        : [];

  const memberColumns: Column<TenantDetailData["members"][number]>[] = [
    {
      key: "who",
      header: "Member",
      cell: (m) => (
        <Link className="text-brand hover:underline" href={`/platform/users/${m.id}?tenant=${tenantId}`}>
          {m.display_name ?? m.email}
          <span className="block text-xs text-muted">{m.email}</span>
        </Link>
      ),
    },
    { key: "status", header: "Membership", cell: (m) => <StatusBadge status={m.status} /> },
  ];

  return (
    <>
      <PageHeader
        title={data.name}
        description={`Slug ${data.slug} · created ${formatDateTime(data.created_at)}`}
        actions={
          <>
            {can("platform:support:request") ? (
              <ButtonLink size="sm" href={`/platform/security/break-glass?tenant=${tenantId}`}>
                Request support access
              </ButtonLink>
            ) : null}
            {manage && data.status === "active" ? (
              <Button size="sm" variant="secondary" onClick={() => setInviting(true)}>
                Invite member
              </Button>
            ) : null}
            {manage
              ? transitions.map((t) => (
                  <Button key={t.status} size="sm" variant={t.tone === "danger" ? "danger" : "primary"} onClick={() => setTransition(t)}>
                    {t.label}
                  </Button>
                ))
              : null}
          </>
        }
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader title="Lifecycle" />
          <div className="px-5 py-4">
            <KeyValues
              items={[
                { label: "Status", value: <StatusBadge status={data.status} /> },
                { label: "Reason", value: data.status_reason ?? "—" },
                { label: "Data region", value: data.data_region },
                { label: "Open invitations", value: data.open_invitations },
                { label: "Version", value: data.version },
                { label: "Updated", value: formatDateTime(data.updated_at) },
              ]}
            />
          </div>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader title="Members" description="Open a member to review effective permissions, MFA and sessions." />
          <DataTable caption="Tenant members" columns={memberColumns} rows={data.members} rowKey={(m) => m.id} empty={<EmptyState title="No members yet" description="The owner joins by accepting the invitation." />} />
        </Card>
      </div>
      <Card className="mt-4">
        <CardHeader title="Support access grants" description="Time-limited, reason-coded and audited. The tenant sees these too." />
        <DataTable
          caption="Support grants"
          rows={data.support_grants}
          rowKey={(g) => g.id}
          columns={[
            { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
            { key: "scope", header: "Scope", cell: (g) => SCOPE_LABEL[g.scope] ?? g.scope },
            { key: "ticket", header: "Ticket", cell: (g) => <span className="font-mono text-xs">{g.ticket_ref}</span> },
            { key: "expires", header: "Expires", cell: (g) => relativeTime(g.expires_at) },
          ]}
          empty={<EmptyState title="No support grants" />}
        />
      </Card>
      <ConfirmDialog
        open={transition !== null}
        onClose={() => setTransition(null)}
        title={`${transition?.label ?? ""} ${data.name}`}
        description={transition?.description}
        confirmLabel={transition?.label ?? "Confirm"}
        tone={transition?.tone ?? "danger"}
        typedConfirmation={transition && transition.status !== "active" ? data.slug : undefined}
        reason={{ minLength: 5, maxLength: 500 }}
        onConfirm={async (reason) => {
          if (!transition) return;
          await api(`/api/v1/platform/tenants/${tenantId}/status`, { method: "POST", body: { status: transition.status, reason } });
          toast({ tone: "success", title: `Tenant ${transition.status === "active" ? "reactivated" : transition.status}` });
          reload();
        }}
      />
      <PlatformInviteDialog
        open={inviting}
        tenantId={tenantId}
        onClose={() => setInviting(false)}
        onInvited={(inv) => {
          setInviting(false);
          reload();
          toast(inv.status === "delivery_failed" ? { tone: "error", title: "Invitation created, delivery failed" } : { tone: "success", title: `Invitation sent to ${inv.email}` });
        }}
      />
    </>
  );
}

function PlatformInviteDialog({ open, tenantId, onClose, onInvited }: { open: boolean; tenantId: string; onClose: () => void; onInvited: (i: Invitation) => void }) {
  const roles = useApi<IamRoles>(open ? "/api/v1/platform/iam/roles" : null);
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const tenantRoles = (roles.data?.roles ?? []).filter((r) => r.scope === "tenant");

  React.useEffect(() => {
    if (open) {
      setEmail("");
      setRole("");
      setError(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onInvited(await api<Invitation>(`/api/v1/platform/tenants/${tenantId}/invitations`, { method: "POST", body: { email: email.trim(), role_keys: [role] } }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Invite a member" description="Platform invitations are audited and require step-up MFA.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="pi-email" label="Email" required error={error?.fields.email}>
          {(p) => <Input {...p} type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />}
        </Field>
        <Field id="pi-role" label="Role" required error={error?.fields.role_keys}>
          {(p) => (
            <Select {...p} required value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="">Choose a role</option>
              {tenantRoles.map((r) => (
                <option key={r.key} value={r.key}>
                  {r.name}
                  {r.requires_mfa ? " (MFA required)" : ""}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !email || !role}>
            {busy ? "Sending…" : "Send invitation"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

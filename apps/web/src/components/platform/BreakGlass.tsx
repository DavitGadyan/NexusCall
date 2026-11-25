"use client";
/**
 * Break-glass / support access (build.md §20.3): request a time-limited, reason-coded, ticketed grant to one
 * tenant; a different administrator approves; grants can be revoked and expire automatically.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import type { Page, SupportGrant, Tenant } from "@/lib/types";
import {
  Button,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Field,
  FormError,
  Input,
  Notice,
  Select,
  StatusBadge,
  StepUpGate,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan, useSession } from "@/components/shell/session-context";
import { SCOPE_LABEL } from "@/components/admin/SupportAccess";

type Pending = { grant: SupportGrant; kind: "approve" | "reject" | "revoke" } | null;

export function BreakGlass({ initialTenant }: { initialTenant?: string }) {
  const can = useCan();
  const session = useSession();
  const toast = useToast();
  const grants = useApi<Page<SupportGrant>>("/api/v1/platform/support-grants");
  const tenants = useApi<Page<Tenant>>("/api/v1/platform/tenants?status=active");
  const [pending, setPending] = React.useState<Pending>(null);
  const tenantName = (id: string) => tenants.data?.items.find((t) => t.id === id)?.name ?? shortId(id);

  const columns: Column<SupportGrant>[] = [
    { key: "tenant", header: "Tenant", cell: (g) => <Link className="text-brand hover:underline" href={`/platform/tenants/${g.tenant_id}`}>{tenantName(g.tenant_id)}</Link> },
    { key: "scope", header: "Scope", cell: (g) => SCOPE_LABEL[g.scope] ?? g.scope },
    { key: "reason", header: "Reason / ticket", cell: (g) => <span className="line-clamp-2 max-w-xs">{g.reason} <span className="font-mono text-xs text-muted">({g.ticket_ref})</span></span> },
    { key: "by", header: "Requested by", cell: (g) => <span className="font-mono text-xs">{g.requested_by === session.user.id ? "you" : shortId(g.requested_by)}</span>, hideOnMobile: true },
    { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
    { key: "expires", header: "Expires", cell: (g) => <span title={formatDateTime(g.expires_at)}>{relativeTime(g.expires_at)}</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (g) => (
        <div className="flex justify-end gap-1">
          {g.status === "pending" && can("platform:support:approve") && g.requested_by !== session.user.id ? (
            <>
              <Button size="sm" onClick={() => setPending({ grant: g, kind: "approve" })}>
                Approve
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setPending({ grant: g, kind: "reject" })}>
                Reject
              </Button>
            </>
          ) : null}
          {g.status === "pending" && g.requested_by === session.user.id ? <span className="self-center text-xs text-muted">Awaiting another approver</span> : null}
          {["pending", "active"].includes(g.status) && can("platform:support:request") ? (
            <Button size="sm" variant="danger" onClick={() => setPending({ grant: g, kind: "revoke" })}>
              Revoke
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <Notice tone="warn" title="Emergency access is exceptional">
        Grants are read-only, scoped to one tenant, limited to the platform maximum (default 4 hours), need a ticket and a second approver, notify the
        tenant, log every read and end automatically. There is no permanent bypass.
      </Notice>
      {can("platform:support:request") ? (
        <RequestForm
          tenants={tenants.data?.items ?? []}
          initialTenant={initialTenant}
          onRequested={() => {
            grants.reload();
            toast({ tone: "success", title: "Access requested", description: "A different administrator must approve it." });
          }}
        />
      ) : null}
      <Card>
        <CardHeader title="Open grants" description="Pending and active grants across all tenants." />
        <DataTable
          caption="Support access grants"
          columns={columns}
          rows={grants.data?.items ?? []}
          rowKey={(g) => g.id}
          loading={grants.loading}
          error={grants.error}
          onRetry={grants.reload}
          empty={<EmptyState title="No open grants" />}
        />
      </Card>
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending?.kind === "approve" ? "Approve support access" : pending?.kind === "reject" ? "Reject support access" : "Revoke support access"}
        description={
          pending?.kind === "approve"
            ? `Read-only access to ${pending ? tenantName(pending.grant.tenant_id) : ""} until ${pending ? formatDateTime(pending.grant.expires_at) : ""}. The tenant is notified.`
            : pending?.kind === "revoke"
              ? "Access ends immediately."
              : "The request is closed without granting access."
        }
        confirmLabel={pending?.kind === "approve" ? "Approve" : pending?.kind === "reject" ? "Reject" : "Revoke"}
        tone={pending?.kind === "approve" ? "primary" : "danger"}
        typedConfirmation={pending?.kind === "approve" ? pending.grant.ticket_ref : undefined}
        onConfirm={async () => {
          if (!pending) return;
          const { grant, kind } = pending;
          const q = `tenant_id=${encodeURIComponent(grant.tenant_id)}`;
          if (kind === "revoke") await api(`/api/v1/platform/support-grants/${grant.id}/revoke?${q}`, { method: "POST" });
          else await api(`/api/v1/platform/support-grants/${grant.id}/decision?${q}`, { method: "POST", body: { approve: kind === "approve", note: "" } });
          toast({ tone: "success", title: kind === "approve" ? "Access approved" : kind === "reject" ? "Request rejected" : "Access revoked" });
          grants.reload();
        }}
      />
    </div>
  );
}

function RequestForm({ tenants, initialTenant, onRequested }: { tenants: Tenant[]; initialTenant?: string; onRequested: () => void }) {
  const [form, setForm] = React.useState({ tenant_id: initialTenant ?? "", scope: "read_config", reason: "", ticket_ref: "", duration_minutes: 60 });
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/platform/support-grants", { method: "POST", body: { ...form, reason: form.reason.trim(), ticket_ref: form.ticket_ref.trim() } });
      setForm({ tenant_id: "", scope: "read_config", reason: "", ticket_ref: "", duration_minutes: 60 });
      onRequested();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const valid = form.tenant_id && form.reason.trim().length >= 10 && /^[A-Za-z0-9_.:#-]{3,64}$/.test(form.ticket_ref.trim());
  return (
    <Card>
      <CardHeader title="Request access" description="Requires step-up MFA. The duration is capped by the platform security policy." />
      <div className="px-5 py-4">
        <StepUpGate description="Requesting emergency access needs a recent multi-factor sign-in.">
          <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
            <Field id="bg-tenant" label="Tenant" required error={error?.fields.tenant_id}>
              {(p) => (
                <Select {...p} required value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}>
                  <option value="">Choose a tenant</option>
                  {tenants.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field id="bg-scope" label="Scope" required error={error?.fields.scope}>
              {(p) => (
                <Select {...p} value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}>
                  {Object.entries(SCOPE_LABEL).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field id="bg-ticket" label="Ticket reference" required hint="e.g. SUP-1234" error={error?.fields.ticket_ref}>
              {(p) => <Input {...p} value={form.ticket_ref} maxLength={64} onChange={(e) => setForm({ ...form, ticket_ref: e.target.value })} />}
            </Field>
            <Field id="bg-duration" label="Duration (minutes)" hint="15 minutes to the platform maximum." error={error?.fields.duration_minutes}>
              {(p) => <Input {...p} type="number" min={15} max={1440} value={form.duration_minutes} onChange={(e) => setForm({ ...form, duration_minutes: Number(e.target.value) })} />}
            </Field>
            <Field id="bg-reason" label="Reason" required hint="At least 10 characters. The tenant sees this." error={error?.fields.reason} className="md:col-span-2">
              {(p) => <Textarea {...p} value={form.reason} maxLength={1000} onChange={(e) => setForm({ ...form, reason: e.target.value })} />}
            </Field>
            <div className="md:col-span-2">
              <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
            </div>
            <div className="flex justify-end md:col-span-2">
              <Button type="submit" disabled={busy || !valid}>
                {busy ? "Requesting…" : "Request access"}
              </Button>
            </div>
          </form>
        </StepUpGate>
      </div>
    </Card>
  );
}

"use client";
/**
 * Service accounts (build.md §18.4): named non-human identities with owner, purpose, review date and scopes
 * from the controlled catalogue. Client secrets go straight to Vault; the UI only ever sees the reference.
 */
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import type { IamRoles, Page, PlatformUser, ServiceAccount, Tenant } from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  Field,
  FormError,
  Input,
  Notice,
  Select,
  StatusBadge,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";

type Pending = { sa: ServiceAccount; kind: "rotate" | "disable" | "enable" } | null;

export function ServiceAccounts() {
  const can = useCan();
  const toast = useToast();
  const list = useApi<Page<ServiceAccount>>("/api/v1/platform/service-accounts");
  const [creating, setCreating] = React.useState(false);
  const [pending, setPending] = React.useState<Pending>(null);
  const manage = can("platform:iam:manage");
  const now = Date.now();

  const columns: Column<ServiceAccount>[] = [
    {
      key: "name",
      header: "Service account",
      cell: (s) => (
        <div className="min-w-0">
          <p className="font-medium">{s.name}</p>
          <p className="font-mono text-xs text-muted">{s.client_id}</p>
        </div>
      ),
    },
    { key: "status", header: "Status", cell: (s) => <StatusBadge status={s.status} /> },
    { key: "scope", header: "Scopes", cell: (s) => <span className="text-xs">{s.scopes.length} scope{s.scopes.length === 1 ? "" : "s"}</span>, hideOnMobile: true },
    { key: "tenant", header: "Tenant", cell: (s) => <span className="font-mono text-xs">{s.tenant_id ? shortId(s.tenant_id) : "Platform"}</span>, hideOnMobile: true },
    {
      key: "review",
      header: "Review",
      cell: (s) => (
        <span title={formatDateTime(s.review_at)} className={new Date(s.review_at).getTime() < now ? "text-danger" : undefined}>
          {relativeTime(s.review_at)}
        </span>
      ),
    },
    { key: "rotated", header: "Rotated", cell: (s) => relativeTime(s.last_rotated_at), hideOnMobile: true },
    { key: "ref", header: "Secret reference", cell: (s) => <span className="font-mono text-xs">{s.secret_ref ?? "—"}</span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (s) =>
        manage ? (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => setPending({ sa: s, kind: "rotate" })}>
              Rotate
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setPending({ sa: s, kind: s.status === "active" ? "disable" : "enable" })}>
              {s.status === "active" ? "Disable" : "Enable"}
            </Button>
          </div>
        ) : null,
    },
  ];

  return (
    <>
      <Notice tone="info" title="Secrets are never shown">
        Client secrets are generated at the identity provider and written directly to Vault. This page shows only the Vault reference.
      </Notice>
      <Card className="mt-4">
        <CardHeader
          title="Service accounts"
          actions={
            manage ? (
              <Button size="sm" onClick={() => setCreating(true)}>
                Create service account
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Service accounts"
          columns={columns}
          rows={list.data?.items ?? []}
          rowKey={(s) => s.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={<EmptyState title="No service accounts" description="Create one for an integration that needs its own identity." />}
        />
      </Card>
      <CreateServiceAccount
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(ref) => {
          setCreating(false);
          list.reload();
          toast({ tone: "success", title: "Service account created", description: `Secret stored in Vault at ${ref}.` });
        }}
      />
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending?.kind === "rotate" ? "Rotate client secret" : pending?.kind === "disable" ? "Disable service account" : "Enable service account"}
        description={
          pending?.kind === "rotate"
            ? "A new secret is generated and written to Vault. The old secret stops working immediately; consumers must read the new one."
            : pending?.kind === "disable"
              ? "The identity can no longer obtain tokens."
              : "The identity can obtain tokens again."
        }
        confirmLabel={pending?.kind === "rotate" ? "Rotate" : pending?.kind === "disable" ? "Disable" : "Enable"}
        tone={pending?.kind === "enable" ? "primary" : "danger"}
        typedConfirmation={pending ? pending.sa.name : undefined}
        onConfirm={async () => {
          if (!pending) return;
          if (pending.kind === "rotate") await api(`/api/v1/platform/service-accounts/${pending.sa.id}/rotate`, { method: "POST" });
          else await api(`/api/v1/platform/service-accounts/${pending.sa.id}/status`, { method: "POST", body: { approve: pending.kind === "enable", note: "" } });
          toast({ tone: "success", title: pending.kind === "rotate" ? "Secret rotated" : `Service account ${pending.kind}d` });
          list.reload();
        }}
      />
    </>
  );
}

function CreateServiceAccount({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (ref: string) => void }) {
  const catalogue = useApi<IamRoles>(open ? "/api/v1/platform/iam/roles" : null);
  const users = useApi<Page<PlatformUser>>(open ? "/api/v1/platform/users" : null);
  const tenants = useApi<Page<Tenant>>(open ? "/api/v1/platform/tenants?status=active" : null);
  const [form, setForm] = React.useState({ name: "", purpose: "", owner_user_id: "", tenant_id: "", review_in_days: 90 });
  const [scopes, setScopes] = React.useState<Set<string>>(new Set());
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (open) {
      setForm({ name: "", purpose: "", owner_user_id: "", tenant_id: "", review_in_days: 90 });
      setScopes(new Set());
      setError(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ secret_ref: string }>("/api/v1/platform/service-accounts", {
        method: "POST",
        body: { ...form, tenant_id: form.tenant_id || null, scopes: [...scopes].sort() },
      });
      onCreated(out.secret_ref);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const perms = Object.entries(catalogue.data?.catalogue.tenant ?? {});
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Create service account" size="xl" description="Grant only the scopes this integration needs. Review dates force a periodic check.">
      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <Field id="sa-name" label="Name" required hint="Lowercase letters, digits and hyphens." error={error?.fields.name}>
          {(p) => <Input {...p} value={form.name} pattern="[a-z0-9-]+" minLength={3} maxLength={80} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} />}
        </Field>
        <Field id="sa-owner" label="Owner" required error={error?.fields.owner_user_id}>
          {(p) => (
            <Select {...p} required value={form.owner_user_id} onChange={(e) => setForm({ ...form, owner_user_id: e.target.value })}>
              <option value="">Choose the accountable person</option>
              {(users.data?.items ?? [])
                .filter((u) => u.status === "active")
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name ?? u.email} ({u.email})
                  </option>
                ))}
            </Select>
          )}
        </Field>
        <Field id="sa-purpose" label="Purpose" required hint="At least 10 characters." error={error?.fields.purpose} className="md:col-span-2">
          {(p) => <Textarea {...p} value={form.purpose} maxLength={500} onChange={(e) => setForm({ ...form, purpose: e.target.value })} />}
        </Field>
        <Field id="sa-tenant" label="Tenant (optional)" hint="Restrict the identity to one tenant." error={error?.fields.tenant_id}>
          {(p) => (
            <Select {...p} value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}>
              <option value="">Platform-wide</option>
              {(tenants.data?.items ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field id="sa-review" label="Review in (days)" hint="7–365 days." error={error?.fields.review_in_days}>
          {(p) => <Input {...p} type="number" min={7} max={365} value={form.review_in_days} onChange={(e) => setForm({ ...form, review_in_days: Number(e.target.value) })} />}
        </Field>
        <fieldset className="md:col-span-2">
          <legend className="mb-2 text-sm font-medium">
            Scopes <Badge>{scopes.size} selected</Badge>
          </legend>
          <div className="grid max-h-60 gap-2 overflow-y-auto rounded-md border border-border p-3 sm:grid-cols-2">
            {perms.map(([key, desc]) => (
              <Checkbox
                key={key}
                checked={scopes.has(key)}
                onChange={(e) => {
                  const next = new Set(scopes);
                  if (e.target.checked) next.add(key);
                  else next.delete(key);
                  setScopes(next);
                }}
                label={<span className="font-mono text-xs">{key}</span>}
                description={desc}
              />
            ))}
          </div>
          {error?.fields.scopes ? <p className="mt-1 text-xs text-danger">{error.fields.scopes}</p> : null}
        </fieldset>
        <div className="md:col-span-2">
          <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        </div>
        <div className="flex justify-end gap-2 md:col-span-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !form.name || form.purpose.trim().length < 10 || !form.owner_user_id || scopes.size === 0}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

"use client";
/** Roles & permissions: role-to-permission mappings, the controlled catalogue, and custom tenant roles. */
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import type { CatalogueRole, IamRoles } from "@/lib/types";
import { Badge, Button, Card, CardHeader, Checkbox, type Column, DataTable, Dialog, EmptyState, ErrorState, Field, FormError, Input, LoadingState, Tabs, Textarea, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";

export function RolesAndPermissions() {
  const can = useCan();
  const toast = useToast();
  const data = useApi<IamRoles>("/api/v1/platform/iam/roles");
  const [tab, setTab] = React.useState("roles");
  const [creating, setCreating] = React.useState(false);

  if (data.loading && !data.data) return <Card><LoadingState /></Card>;
  if (data.error && !data.data) return <Card><ErrorState problem={data.error.problem} onRetry={data.reload} /></Card>;
  const roles = data.data?.roles ?? [];
  const catalogue = data.data?.catalogue ?? { tenant: {}, platform: {} };

  const roleColumns: Column<CatalogueRole>[] = [
    {
      key: "role",
      header: "Role",
      cell: (r) => (
        <div>
          <p className="font-medium">{r.name}</p>
          <p className="font-mono text-xs text-muted">{r.key}</p>
        </div>
      ),
    },
    { key: "scope", header: "Scope", cell: (r) => <Badge tone={r.scope === "platform" ? "brand" : "neutral"}>{r.scope}</Badge> },
    { key: "kind", header: "Type", cell: (r) => (r.is_system ? "System" : "Custom"), hideOnMobile: true },
    { key: "mfa", header: "MFA", cell: (r) => (r.requires_mfa ? <Badge tone="warn">Required</Badge> : "—"), hideOnMobile: true },
    {
      key: "perms",
      header: "Permissions",
      cell: (r) => (
        <details>
          <summary className="cursor-pointer text-sm text-brand">{r.permissions.length} permissions</summary>
          <ul className="mt-1 flex flex-col gap-0.5">
            {r.permissions.map((p) => (
              <li key={p} className="font-mono text-xs">
                {p}
              </li>
            ))}
          </ul>
        </details>
      ),
    },
  ];

  const catalogueRows = [
    ...Object.entries(catalogue.tenant).map(([key, description]) => ({ key, description, scope: "tenant" })),
    ...Object.entries(catalogue.platform).map(([key, description]) => ({ key, description, scope: "platform" })),
  ];

  return (
    <>
      <Tabs
        label="Roles and permissions"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "roles", label: "Roles" },
          { id: "catalogue", label: "Permission catalogue" },
        ]}
      >
        {tab === "roles" ? (
          <Card>
            <CardHeader
              title="Roles"
              description="System roles are defined in code. Custom tenant roles can only combine catalogue permissions."
              actions={
                can("platform:iam:manage") ? (
                  <Button size="sm" onClick={() => setCreating(true)}>
                    Create custom role
                  </Button>
                ) : null
              }
            />
            <DataTable caption="Roles" columns={roleColumns} rows={roles} rowKey={(r) => r.key} empty={<EmptyState title="No roles" />} />
          </Card>
        ) : (
          <Card>
            <CardHeader title="Permission catalogue" description="The only permissions roles may contain. There are no free-form permissions." />
            <DataTable
              caption="Permission catalogue"
              rows={catalogueRows}
              rowKey={(r) => r.key}
              columns={[
                { key: "key", header: "Permission", cell: (r) => <span className="font-mono text-xs">{r.key}</span> },
                { key: "scope", header: "Scope", cell: (r) => <Badge tone={r.scope === "platform" ? "brand" : "neutral"}>{r.scope}</Badge> },
                { key: "desc", header: "Description", cell: (r) => r.description },
              ]}
            />
          </Card>
        )}
      </Tabs>
      <CreateRoleDialog
        open={creating}
        tenantCatalogue={catalogue.tenant}
        onClose={() => setCreating(false)}
        onCreated={(key) => {
          setCreating(false);
          data.reload();
          toast({ tone: "success", title: `Role ${key} created` });
        }}
      />
    </>
  );
}

function CreateRoleDialog({
  open,
  tenantCatalogue,
  onClose,
  onCreated,
}: {
  open: boolean;
  tenantCatalogue: Record<string, string>;
  onClose: () => void;
  onCreated: (key: string) => void;
}) {
  const [form, setForm] = React.useState({ key: "", name: "", description: "", requires_mfa: false });
  const [perms, setPerms] = React.useState<Set<string>>(new Set());
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setForm({ key: "", name: "", description: "", requires_mfa: false });
      setPerms(new Set());
      setError(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ key: string }>("/api/v1/platform/iam/roles", { method: "POST", body: { ...form, permissions: [...perms].sort() } });
      onCreated(out.key);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Create custom tenant role" size="xl" description="Available to every tenant. Admins can grant it only if they hold all of its permissions.">
      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <Field id="role-key" label="Key" required hint="Lowercase, e.g. quality_reviewer." error={error?.fields.key}>
          {(p) => <Input {...p} className="font-mono" value={form.key} pattern="[a-z][a-z0-9_]{2,63}" onChange={(e) => setForm({ ...form, key: e.target.value.toLowerCase() })} />}
        </Field>
        <Field id="role-name" label="Name" required error={error?.fields.name}>
          {(p) => <Input {...p} value={form.name} maxLength={120} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        </Field>
        <Field id="role-desc" label="Description" error={error?.fields.description} className="md:col-span-2">
          {(p) => <Textarea {...p} value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} />}
        </Field>
        <Checkbox className="md:col-span-2" label="Requires MFA" description="Members with this role must use multi-factor authentication." checked={form.requires_mfa} onChange={(e) => setForm({ ...form, requires_mfa: e.target.checked })} />
        <fieldset className="md:col-span-2">
          <legend className="mb-2 text-sm font-medium">Permissions ({perms.size})</legend>
          <div className="grid max-h-64 gap-2 overflow-y-auto rounded-md border border-border p-3 sm:grid-cols-2">
            {Object.entries(tenantCatalogue).map(([key, desc]) => (
              <Checkbox
                key={key}
                checked={perms.has(key)}
                onChange={(e) => {
                  const next = new Set(perms);
                  if (e.target.checked) next.add(key);
                  else next.delete(key);
                  setPerms(next);
                }}
                label={<span className="font-mono text-xs">{key}</span>}
                description={desc}
              />
            ))}
          </div>
          {error?.fields.permissions ? <p className="mt-1 text-xs text-danger">{error.fields.permissions}</p> : null}
        </fieldset>
        <div className="md:col-span-2">
          <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        </div>
        <div className="flex justify-end gap-2 md:col-span-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !form.key || form.name.length < 2 || perms.size === 0}>
            {busy ? "Creating…" : "Create role"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

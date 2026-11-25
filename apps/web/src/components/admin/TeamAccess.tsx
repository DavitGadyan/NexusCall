"use client";
/**
 * Team & access (Tenant Admin Console): members, role editor honouring `grantable`, deactivation,
 * optimistic-lock conflicts, roles catalogue and teams. Every change is re-authorised and audited server-side.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { type ApiState, useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Membership, Page, PermissionInfo, Team, TenantRole } from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  ConfirmDialog,
  ConflictNotice,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  Input,
  LoadingState,
  Notice,
  Select,
  StatusBadge,
  Tabs,
  useToast,
} from "@/components/ui";
import { useSession } from "@/components/shell/session-context";

export function TeamAccess() {
  const [tab, setTab] = React.useState("members");
  const members = useApi<Page<Membership>>("/api/v1/memberships");
  const roles = useApi<Page<TenantRole>>("/api/v1/roles");
  const teams = useApi<Page<Team>>("/api/v1/teams");
  return (
    <Tabs
      label="Team and access"
      value={tab}
      onChange={setTab}
      tabs={[
        { id: "members", label: "Members", badge: members.data ? <Badge>{members.data.items.length}</Badge> : null },
        { id: "roles", label: "Roles" },
        { id: "teams", label: "Teams" },
      ]}
    >
      {tab === "members" ? <Members members={members} roles={roles.data?.items ?? []} teams={teams.data?.items ?? []} /> : null}
      {tab === "roles" ? <RolesCatalogue roles={roles} /> : null}
      {tab === "teams" ? <Teams teams={teams} /> : null}
    </Tabs>
  );
}

type ApiResource<T> = ApiState<T>;

function Members({ members, roles, teams }: { members: ApiResource<Page<Membership>>; roles: TenantRole[]; teams: Team[] }) {
  const session = useSession();
  const toast = useToast();
  const [q, setQ] = React.useState("");
  const [editing, setEditing] = React.useState<Membership | null>(null);
  const [toggling, setToggling] = React.useState<Membership | null>(null);
  const roleName = (k: string) => roles.find((r) => r.key === k)?.name ?? k;
  const teamName = (id: string | null) => (id ? (teams.find((t) => t.id === id)?.name ?? "—") : "—");
  const rows = (members.data?.items ?? []).filter((m) => {
    const needle = q.trim().toLowerCase();
    return !needle || m.email.toLowerCase().includes(needle) || (m.display_name ?? "").toLowerCase().includes(needle);
  });

  const columns: Column<Membership>[] = [
    {
      key: "person",
      header: "Member",
      cell: (m) => (
        <div className="min-w-0">
          <p className="truncate font-medium">{m.display_name ?? m.email}</p>
          <p className="truncate text-xs text-muted">{m.email}</p>
        </div>
      ),
    },
    {
      key: "roles",
      header: "Roles",
      cell: (m) => (
        <div className="flex flex-wrap gap-1">
          {m.roles.length ? m.roles.map((r) => <Badge key={r} tone={r === "tenant_owner" ? "brand" : "neutral"}>{roleName(r)}</Badge>) : <span className="text-muted">None</span>}
        </div>
      ),
    },
    { key: "team", header: "Team", cell: (m) => teamName(m.team_id), hideOnMobile: true },
    { key: "status", header: "Status", cell: (m) => <StatusBadge status={m.status} /> },
    { key: "seen", header: "Last seen", cell: (m) => <span title={formatDateTime(m.last_seen_at)}>{relativeTime(m.last_seen_at)}</span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (m) => {
        const self = m.user_id === session.user.id;
        return self ? (
          <span className="text-xs text-muted">You</span>
        ) : (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => setEditing(m)}>
              Edit access
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setToggling(m)}>
              {m.status === "active" ? "Deactivate" : "Reactivate"}
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Members"
        description="People with access to this workspace. You cannot change your own access."
        actions={
          <Link href="/admin/invitations" className="text-sm font-medium text-brand hover:underline">
            Invite people
          </Link>
        }
      />
      <div className="border-b border-border px-4 py-3">
        <label className="sr-only" htmlFor="member-search">
          Search members
        </label>
        <Input id="member-search" placeholder="Search by name or email" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-sm" />
      </div>
      <DataTable
        caption="Members"
        columns={columns}
        rows={rows}
        rowKey={(m) => m.id}
        loading={members.loading}
        error={members.error}
        onRetry={members.reload}
        empty={<EmptyState title={q ? "No matching members" : "No members yet"} description={q ? "Try another search." : "Invite people to get started."} />}
      />
      <RoleEditor
        member={editing}
        roles={roles}
        teams={teams}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          members.reload();
          toast({ tone: "success", title: "Access updated" });
        }}
        onConflict={() => {
          setEditing(null);
          members.reload();
        }}
      />
      <ConfirmDialog
        open={toggling !== null}
        onClose={() => setToggling(null)}
        title={toggling?.status === "active" ? "Deactivate member" : "Reactivate member"}
        description={
          toggling?.status === "active"
            ? "They lose access to this workspace immediately. Their audit history is kept."
            : "They regain the access their roles grant."
        }
        confirmLabel={toggling?.status === "active" ? "Deactivate" : "Reactivate"}
        tone={toggling?.status === "active" ? "danger" : "primary"}
        typedConfirmation={toggling?.status === "active" ? toggling.email : undefined}
        onConfirm={async () => {
          if (!toggling) return;
          try {
            await api(`/api/v1/memberships/${toggling.id}`, {
              method: "PATCH",
              body: { status: toggling.status === "active" ? "deactivated" : "active", expected_version: toggling.version },
            });
          } catch (err) {
            const e = toApiError(err);
            if (e.isConflict) members.reload();
            throw e;
          }
          members.reload();
          toast({ tone: "success", title: toggling.status === "active" ? "Member deactivated" : "Member reactivated" });
        }}
      />
    </Card>
  );
}

function RoleEditor({
  member,
  roles,
  teams,
  onClose,
  onSaved,
  onConflict,
}: {
  member: Membership | null;
  roles: TenantRole[];
  teams: Team[];
  onClose: () => void;
  onSaved: () => void;
  onConflict: () => void;
}) {
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [team, setTeam] = React.useState<string>("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (member) {
      setSelected(new Set(member.roles));
      setTeam(member.team_id ?? "");
      setError(null);
    }
  }, [member]);

  if (!member) return <Dialog open={false} onClose={onClose} title="Edit access" />;
  const heldAbove = member.roles.filter((k) => roles.find((r) => r.key === k && !r.grantable));
  const locked = heldAbove.length > 0;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!member) return;
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = { expected_version: member.version };
    const keys = [...selected].sort();
    if (keys.join(",") !== [...member.roles].sort().join(",")) body.role_keys = keys;
    if ((member.team_id ?? "") !== team) body.team_id = team || null;
    try {
      await api(`/api/v1/memberships/${member.id}`, { method: "PATCH", body });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title={`Edit access: ${member.display_name ?? member.email}`} size="lg">
      <form onSubmit={save} className="flex flex-col gap-4">
        {locked ? (
          <Notice tone="warn" title="This member holds roles you cannot grant">
            Their access includes permissions you do not have ({heldAbove.join(", ")}). Ask a workspace owner to change it.
          </Notice>
        ) : null}
        {error?.isConflict ? <ConflictNotice onReload={onConflict} /> : <FormError message={error && !error.isStepUp ? error.message : null} />}
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Roles</legend>
          {roles.map((r) => (
            <Checkbox
              key={r.key}
              checked={selected.has(r.key)}
              disabled={locked || !r.grantable}
              onChange={(e) =>
                setSelected((prev) => {
                  const next = new Set(prev);
                  if (e.target.checked) next.add(r.key);
                  else next.delete(r.key);
                  return next;
                })
              }
              label={
                <span className="inline-flex flex-wrap items-center gap-2">
                  {r.name}
                  {r.requires_mfa ? <Badge tone="brand">MFA required</Badge> : null}
                  {!r.grantable ? <Badge>Not grantable by you</Badge> : null}
                </span>
              }
              description={r.grantable ? r.description : `${r.description} — it includes permissions you do not hold.`}
            />
          ))}
          {error?.fields.role_keys ? <p className="text-xs text-danger">{error.fields.role_keys}</p> : null}
        </fieldset>
        <Field id="member-team" label="Team" error={error?.fields.team_id}>
          {(p) => (
            <Select {...p} value={team} onChange={(e) => setTeam(e.target.value)} disabled={locked}>
              <option value="">No team</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || locked || selected.size === 0}>
            {busy ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RolesCatalogue({ roles }: { roles: ApiResource<Page<TenantRole>> }) {
  const perms = useApi<Page<PermissionInfo>>("/api/v1/permissions");
  const describe = (k: string) => perms.data?.items.find((p) => p.key === k)?.description ?? k;
  if (roles.loading) return <LoadingState />;
  if (roles.error) return <ErrorState problem={roles.error.problem} onRetry={roles.reload} />;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {(roles.data?.items ?? []).map((r) => (
        <Card key={r.key}>
          <CardHeader
            title={r.name}
            description={r.description}
            actions={
              <div className="flex gap-1">
                {r.requires_mfa ? <Badge tone="brand">MFA</Badge> : null}
                <Badge tone={r.grantable ? "ok" : "neutral"}>{r.grantable ? "You can grant" : "Not grantable"}</Badge>
              </div>
            }
          />
          <ul className="flex flex-col gap-1 px-5 py-3 text-sm">
            {r.permissions.map((p) => (
              <li key={p} className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-xs">{p}</span>
                <span className="text-xs text-muted">{describe(p)}</span>
              </li>
            ))}
          </ul>
        </Card>
      ))}
    </div>
  );
}

function Teams({ teams }: { teams: ApiResource<Page<Team>> }) {
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const toast = useToast();

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/teams", { method: "POST", body: { name: name.trim() } });
      setName("");
      teams.reload();
      toast({ tone: "success", title: "Team created" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Teams" description="Group members for assignment and reporting." />
      <form onSubmit={create} className="flex flex-wrap items-end gap-2 border-b border-border px-4 py-3">
        <Field id="team-name" label="New team" error={error?.fields.name ?? (error && !error.fields.name ? error.message : null)} className="w-full max-w-xs">
          {(p) => <Input {...p} value={name} minLength={2} maxLength={120} required onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Button type="submit" disabled={busy || name.trim().length < 2}>
          {busy ? "Creating…" : "Create team"}
        </Button>
      </form>
      <DataTable
        caption="Teams"
        columns={[{ key: "name", header: "Name", cell: (t: Team) => t.name }]}
        rows={teams.data?.items ?? []}
        rowKey={(t) => t.id}
        loading={teams.loading}
        error={teams.error}
        onRetry={teams.reload}
        empty={<EmptyState title="No teams yet" />}
      />
    </Card>
  );
}

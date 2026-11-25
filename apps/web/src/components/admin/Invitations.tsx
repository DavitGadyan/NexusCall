"use client";
/**
 * Invitations (build.md §18.3): invite, bulk CSV with per-row validation and a downloadable result report,
 * resend (supersedes the old token), revoke. The server re-checks which roles the admin may grant.
 */
import { Download, Upload } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { downloadText } from "@/lib/client/download";
import { type ApiState, useApi } from "@/lib/client/use-api";
import { type InviteRow, MAX_BULK_ROWS, parseInviteCsv, toCsv } from "@/lib/csv";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { BulkResult, Invitation, InvitationStatus, Page, Team, TenantRole } from "@/lib/types";
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

const OPEN: InvitationStatus[] = ["pending", "sent", "delivery_failed"];
const STATUSES: InvitationStatus[] = ["pending", "sent", "delivery_failed", "accepted", "expired", "revoked"];

export function Invitations() {
  const toast = useToast();
  const [status, setStatus] = React.useState<string>("");
  const list = useApi<Page<Invitation>>(withQuery("/api/v1/invitations", { status }));
  const roles = useApi<Page<TenantRole>>("/api/v1/roles");
  const teams = useApi<Page<Team>>("/api/v1/teams");
  const [inviteOpen, setInviteOpen] = React.useState(false);
  const [bulkOpen, setBulkOpen] = React.useState(false);
  const [revoking, setRevoking] = React.useState<Invitation | null>(null);
  const [resending, setResending] = React.useState<string | null>(null);
  const roleName = (k: string) => roles.data?.items.find((r) => r.key === k)?.name ?? k;

  async function resend(inv: Invitation) {
    setResending(inv.id);
    try {
      const out = await api<Invitation>(`/api/v1/invitations/${inv.id}/resend`, { method: "POST" });
      toast(
        out.status === "delivery_failed"
          ? { tone: "error", title: "Invitation re-issued, but delivery failed", description: out.delivery_error ?? undefined }
          : { tone: "success", title: "Invitation re-sent", description: "The previous link no longer works." },
      );
      list.reload();
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Could not resend", description: e.message });
    } finally {
      setResending(null);
    }
  }

  const columns: Column<Invitation>[] = [
    { key: "email", header: "Email", cell: (i) => <span className="font-medium">{i.email}</span> },
    { key: "roles", header: "Roles", cell: (i) => <div className="flex flex-wrap gap-1">{i.role_keys.map((r) => <Badge key={r}>{roleName(r)}</Badge>)}</div> },
    {
      key: "status",
      header: "Status",
      cell: (i) => (
        <div className="flex flex-col gap-0.5">
          <StatusBadge status={i.status} />
          {i.delivery_error ? <span className="text-xs text-danger">{i.delivery_error}</span> : null}
        </div>
      ),
    },
    { key: "sent", header: "Sent", cell: (i) => <span title={formatDateTime(i.sent_at)}>{i.sent_at ? `${relativeTime(i.sent_at)} (${i.send_count}×)` : "—"}</span>, hideOnMobile: true },
    { key: "expires", header: "Expires", cell: (i) => <span title={formatDateTime(i.expires_at)}>{relativeTime(i.expires_at)}</span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (i) =>
        OPEN.includes(i.status) ? (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => resend(i)} disabled={resending === i.id}>
              {resending === i.id ? "Sending…" : "Resend"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRevoking(i)}>
              Revoke
            </Button>
          </div>
        ) : null,
    },
  ];

  return (
    <>
      <Card>
        <CardHeader
          title="Invitations"
          description="Links are single-use and expire. Resending issues a new link and invalidates the old one."
          actions={
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => setBulkOpen(true)}>
                <Upload className="h-4 w-4" aria-hidden /> Bulk invite
              </Button>
              <Button size="sm" onClick={() => setInviteOpen(true)} data-testid="open-invite">
                Invite
              </Button>
            </div>
          }
        />
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
          <label htmlFor="inv-status" className="text-sm text-muted">
            Status
          </label>
          <Select id="inv-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-48">
            <option value="">All</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")}
              </option>
            ))}
          </Select>
        </div>
        <DataTable
          caption="Invitations"
          columns={columns}
          rows={list.data?.items ?? []}
          rowKey={(i) => i.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={<EmptyState title="No invitations" description="Invite someone to join this workspace." />}
        />
      </Card>
      <InviteDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        roles={roles}
        teams={teams.data?.items ?? []}
        onInvited={(inv) => {
          setInviteOpen(false);
          list.reload();
          toast(
            inv.status === "delivery_failed"
              ? { tone: "error", title: "Invitation created, but the email could not be delivered", description: "Use Resend to try again." }
              : { tone: "success", title: `Invitation sent to ${inv.email}` },
          );
        }}
      />
      <BulkInviteDialog open={bulkOpen} onClose={() => setBulkOpen(false)} roles={roles.data?.items ?? []} teams={teams.data?.items ?? []} onDone={list.reload} />
      <ConfirmDialog
        open={revoking !== null}
        onClose={() => setRevoking(null)}
        title="Revoke invitation"
        description={`The link sent to ${revoking?.email ?? ""} stops working immediately.`}
        confirmLabel="Revoke"
        onConfirm={async () => {
          if (!revoking) return;
          await api(`/api/v1/invitations/${revoking.id}/revoke`, { method: "POST" });
          list.reload();
          toast({ tone: "success", title: "Invitation revoked" });
        }}
      />
    </>
  );
}

function RolePicker({ roles, selected, onChange, error }: { roles: TenantRole[]; selected: Set<string>; onChange: (s: Set<string>) => void; error?: string }) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">Roles</legend>
      {roles.map((r) => (
        <Checkbox
          key={r.key}
          checked={selected.has(r.key)}
          disabled={!r.grantable}
          onChange={(e) => {
            const next = new Set(selected);
            if (e.target.checked) next.add(r.key);
            else next.delete(r.key);
            onChange(next);
          }}
          label={
            <span className="inline-flex flex-wrap items-center gap-2">
              {r.name}
              {r.requires_mfa ? <Badge tone="brand">MFA required</Badge> : null}
              {!r.grantable ? <Badge>Not grantable by you</Badge> : null}
            </span>
          }
          description={r.description}
        />
      ))}
      {error ? <p className="text-xs text-danger">{error}</p> : null}
    </fieldset>
  );
}

function InviteDialog({
  open,
  onClose,
  roles,
  teams,
  onInvited,
}: {
  open: boolean;
  onClose: () => void;
  roles: ApiState<Page<TenantRole>>;
  teams: Team[];
  onInvited: (inv: Invitation) => void;
}) {
  const [email, setEmail] = React.useState("");
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [team, setTeam] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (open) {
      setEmail("");
      setSelected(new Set());
      setTeam("");
      setError(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api<Invitation>("/api/v1/invitations", {
        method: "POST",
        body: { email: email.trim(), role_keys: [...selected].sort(), ...(team ? { team_id: team } : {}) },
      });
      onInvited(inv);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const adminRole = [...selected].some((k) => roles.data?.items.find((r) => r.key === k)?.requires_mfa);
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Invite a person" description="They receive a single-use link by email." size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="invite-email" label="Email" required error={error?.fields.email}>
          {(p) => <Input {...p} type="email" autoComplete="off" required value={email} onChange={(e) => setEmail(e.target.value)} data-testid="invite-email" />}
        </Field>
        {roles.loading ? <p className="text-sm text-muted">Loading roles…</p> : <RolePicker roles={roles.data?.items ?? []} selected={selected} onChange={setSelected} error={error?.fields.role_keys} />}
        {adminRole ? <Notice tone="info">This role requires multi-factor authentication. The invitee sets up an authenticator before accepting.</Notice> : null}
        <Field id="invite-team" label="Team (optional)" error={error?.fields.team_id}>
          {(p) => (
            <Select {...p} value={team} onChange={(e) => setTeam(e.target.value)}>
              <option value="">No team</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
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
          <Button type="submit" disabled={busy || !email || selected.size === 0} data-testid="send-invite">
            {busy ? "Sending…" : "Send invitation"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

type ReportRow = { row: number; email: string; result: string; status: string; invitation_id: string; error: string };

function BulkInviteDialog({ open, onClose, roles, teams, onDone }: { open: boolean; onClose: () => void; roles: TenantRole[]; teams: Team[]; onDone: () => void }) {
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [report, setReport] = React.useState<ReportRow[] | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (open) {
      setText("");
      setReport(null);
      setError(null);
    }
  }, [open]);

  const parsed = React.useMemo(
    () =>
      parseInviteCsv(text, {
        knownRoles: new Set(roles.map((r) => r.key)),
        grantable: new Set(roles.filter((r) => r.grantable).map((r) => r.key)),
        teams: new Set(teams.map((t) => t.name)),
      }),
    [text, roles, teams],
  );
  const valid = parsed.rows.filter((r) => r.errors.length === 0);
  const invalid = parsed.rows.filter((r) => r.errors.length > 0);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 512 * 1024) {
      setError(toApiError(new Error("The file is larger than 512 KB.")));
      return;
    }
    setText(await file.text());
  }

  async function submit() {
    setBusy(true);
    setError(null);
    const teamId = (name: string | null) => (name ? teams.find((t) => t.name === name)?.id : undefined);
    const clientRows: ReportRow[] = invalid.map((r) => ({ row: r.line, email: r.email, result: "error", status: "", invitation_id: "", error: r.errors.join("; ") }));
    try {
      let serverRows: ReportRow[] = [];
      if (valid.length > 0) {
        const out = await api<BulkResult>("/api/v1/invitations/bulk", {
          method: "POST",
          body: { rows: valid.map((r: InviteRow) => ({ email: r.email, role_keys: r.roleKeys, ...(teamId(r.team) ? { team_id: teamId(r.team) } : {}) })) },
        });
        serverRows = out.rows.map((r) => ({
          row: valid[r.row - 1]?.line ?? r.row,
          email: r.email,
          result: r.result,
          status: r.status ?? "",
          invitation_id: r.invitation_id ?? "",
          error: r.error ?? "",
        }));
      }
      setReport([...serverRows, ...clientRows].sort((a, b) => a.row - b.row));
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  function downloadReport() {
    if (!report) return;
    downloadText(
      `invitation-report-${new Date().toISOString().slice(0, 10)}.csv`,
      toCsv([["row", "email", "result", "status", "invitation_id", "error"], ...report.map((r) => [r.row, r.email, r.result, r.status, r.invitation_id, r.error])]),
    );
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Bulk invite" size="xl" description={`CSV columns: email, roles (separated by ;), team (optional). Up to ${MAX_BULK_ROWS} rows.`}>
      {report ? (
        <div className="flex flex-col gap-4">
          <Notice tone={report.some((r) => r.result !== "invited") ? "warn" : "ok"} title="Bulk invitation finished">
            {report.filter((r) => r.result === "invited").length} invited, {report.filter((r) => r.result !== "invited").length} failed.
          </Notice>
          <ReportTable rows={report} />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={downloadReport}>
              <Download className="h-4 w-4" aria-hidden /> Download report (CSV)
            </Button>
            <Button onClick={onClose}>Done</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2">
              <Upload className="h-4 w-4" aria-hidden /> Choose CSV file
              <input type="file" accept=".csv,text/csv" className="sr-only" onChange={onFile} />
            </label>
            <span className="text-xs text-muted">or paste below</span>
          </div>
          <Field id="bulk-csv" label="CSV content">
            {(p) => <Textarea {...p} rows={6} className="font-mono text-xs" placeholder={"email,roles,team\nana@example.test,analyst,\nbo@example.test,campaign_manager;analyst,Sales"} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          {parsed.fileErrors.map((m) => (
            <FormError key={m} message={m} />
          ))}
          {text && parsed.rows.length > 0 ? (
            <div>
              <p className="mb-2 text-sm">
                <span className="font-medium">{valid.length}</span> ready, <span className="font-medium text-danger">{invalid.length}</span> with problems
                (rows with problems are skipped and listed in the report).
              </p>
              <div className="max-h-64 overflow-auto rounded-md border border-border">
                <table className="w-full text-left text-xs">
                  <caption className="sr-only">CSV preview</caption>
                  <thead>
                    <tr className="text-muted">
                      <th className="px-3 py-1.5">Line</th>
                      <th className="px-3 py-1.5">Email</th>
                      <th className="px-3 py-1.5">Roles</th>
                      <th className="px-3 py-1.5">Problems</th>
                    </tr>
                  </thead>
                  <tbody>
                    {parsed.rows.map((r) => (
                      <tr key={r.line} className="border-t border-border">
                        <td className="px-3 py-1.5 tabular">{r.line}</td>
                        <td className="px-3 py-1.5">{r.email || "—"}</td>
                        <td className="px-3 py-1.5">{r.roleKeys.join(", ") || "—"}</td>
                        <td className={r.errors.length ? "px-3 py-1.5 text-danger" : "px-3 py-1.5 text-ok"}>{r.errors.join("; ") || "OK"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
          <FormError message={error && !error.isStepUp ? error.message : null} />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={submit} disabled={busy || parsed.rows.length === 0 || parsed.fileErrors.some((m) => m.startsWith("At most"))}>
              {busy ? "Sending…" : `Send ${valid.length} invitation${valid.length === 1 ? "" : "s"}`}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function ReportTable({ rows }: { rows: ReportRow[] }) {
  return (
    <div className="max-h-72 overflow-auto rounded-md border border-border">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">Bulk invitation result</caption>
        <thead>
          <tr className="text-muted">
            <th className="px-3 py-1.5">Line</th>
            <th className="px-3 py-1.5">Email</th>
            <th className="px-3 py-1.5">Result</th>
            <th className="px-3 py-1.5">Details</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.row}-${r.email}`} className="border-t border-border">
              <td className="px-3 py-1.5 tabular">{r.row}</td>
              <td className="px-3 py-1.5">{r.email}</td>
              <td className="px-3 py-1.5">
                <StatusBadge status={r.result === "invited" ? (r.status || "sent") : "failure"} />
              </td>
              <td className="px-3 py-1.5 text-muted">{r.error || r.invitation_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

"use client";
/**
 * One MCP server: identity, reviewed pin, limits and change history. Editing is optimistic-locked
 * (`expected_version`); changing an identity field clears the pin and disables the server until it is
 * re-pinned. Enable needs a reviewed pin; disable and revoke need a reason; revoke is permanent.
 */
import { Pencil, Pin, Power, ShieldAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, shortId } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  ConfirmDialog,
  ConflictNotice,
  Dialog,
  ErrorState,
  Field,
  FormError,
  Input,
  KeyValues,
  LoadingState,
  Notice,
  Select,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { Mono, StepUpHint } from "./common";
import { enableBlocker, isPinned, type McpServerDetail as Detail, type PinForm, type ServerForm, serverPatch, serverState, serverToForm, TRUST_LEVELS, validatePin } from "./lib/mcp";

export function McpServerDetail({ serverId }: { serverId: string }) {
  const can = useCan();
  const toast = useToast();
  const srv = useApi<Detail>(`/api/v1/platform/ai/mcp-servers/${serverId}`);
  const [editing, setEditing] = React.useState(false);
  const [pinning, setPinning] = React.useState(false);
  const [pending, setPending] = React.useState<"disable" | "revoke" | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const canManage = can("platform:ai:manage");

  if (srv.loading && !srv.data) return <Card><LoadingState label="Loading server" /></Card>;
  if (srv.error && !srv.data) return <Card><ErrorState problem={srv.error.problem} onRetry={srv.reload} /></Card>;
  const s = srv.data;
  if (!s) return null;
  const st = serverState(s);
  const blocker = enableBlocker(s);

  async function enable() {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/mcp-servers/${serverId}/enable`, { method: "POST" });
      toast({ tone: "success", title: "Server enabled" });
      srv.reload();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Link href="/platform/ai/tools" className="text-sm text-brand hover:underline">
        ← MCP servers
      </Link>
      <Card>
        <CardHeader title={s.display_name} description={`${s.key} · ${s.environment} · ${s.transport}`} actions={<Badge tone={st.tone}>{st.label}</Badge>} />
        <div className="flex flex-col gap-4 px-5 py-4">
          {s.revoked ? <Notice tone="danger" title="Revoked">{s.revoked_reason ?? "This server was revoked. Register a new one to replace it."}</Notice> : null}
          {!s.revoked && s.disabled_reason ? <Notice tone="warn" title="Disabled">{s.disabled_reason}</Notice> : null}
          {s.health === "identity_mismatch" ? <Notice tone="danger" title="Identity mismatch">The server reported a different name, version or hash than the reviewed pin. Review it and pin again before enabling.</Notice> : null}
          <FormError message={error && !error.isStepUp ? error.message : null} />
          <KeyValues
            items={[
              { label: "Endpoint", value: <Mono>{s.endpoint}</Mono> },
              { label: "Allowed hosts", value: <Mono>{s.allowed_hosts.join(", ")}</Mono> },
              { label: "Service identity", value: <Mono>{s.service_identity}</Mono> },
              { label: "Credential reference", value: <Mono>{s.vault_ref ?? "none"}</Mono> },
              { label: "Trust level", value: TRUST_LEVELS.find((t) => t.value === s.trust_level)?.label ?? s.trust_level },
              { label: "Owner", value: s.owner },
              { label: "Purpose", value: s.purpose },
              { label: "Health", value: `${s.health.replace(/_/g, " ")}${s.last_verified_at ? ` · verified ${formatDateTime(s.last_verified_at)}` : ""}` },
              { label: "Limits", value: `${s.rate_per_minute}/min · ${s.max_concurrency} concurrent · ${s.timeout_ms} ms · request ≤ ${s.max_request_bytes} B · response ≤ ${s.max_response_bytes} B` },
            ]}
          />
        </div>
        {canManage && !s.revoked ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-5 py-3">
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)} disabled={busy}>
              <Pencil className="h-4 w-4" aria-hidden /> Edit
            </Button>
            {s.enabled ? (
              <Button size="sm" variant="secondary" onClick={() => setPending("disable")} disabled={busy}>
                <Power className="h-4 w-4" aria-hidden /> Disable
              </Button>
            ) : (
              <span title={blocker ?? undefined}>
                <Button size="sm" variant="secondary" onClick={enable} disabled={busy || blocker !== null} data-testid="enable-server">
                  <Power className="h-4 w-4" aria-hidden /> {busy ? "Enabling…" : "Enable"}
                </Button>
              </span>
            )}
            <Button size="sm" variant="danger" onClick={() => setPending("revoke")} disabled={busy} className="ml-auto">
              <ShieldAlert className="h-4 w-4" aria-hidden /> Revoke
            </Button>
            {!s.enabled && blocker ? <span className="w-full text-xs text-muted">{blocker}</span> : null}
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title="Reviewed pin"
          description="The exact server name, version and SHA-256 a reviewer approved. The gateway refuses a server that reports anything else."
          actions={
            canManage && !s.revoked ? (
              <Button size="sm" variant="secondary" onClick={() => setPinning(true)} data-testid="pin-server">
                <Pin className="h-4 w-4" aria-hidden /> {isPinned(s) ? "Re-pin" : "Pin version"}
              </Button>
            ) : null
          }
        />
        <div className="px-5 py-4">
          {isPinned(s) ? (
            <KeyValues
              items={[
                { label: "Server name", value: <Mono>{s.pinned_server_name ?? "—"}</Mono> },
                { label: "Version", value: <Mono>{s.pinned_version}</Mono> },
                { label: "SHA-256", value: <Mono>{s.pinned_sha256}</Mono> },
                { label: "Reviewed", value: `${formatDateTime(s.reviewed_at)} by ${shortId(s.reviewed_by)}` },
              ]}
            />
          ) : (
            <Notice tone="warn">Not pinned. The server cannot be enabled until a reviewed version is pinned.</Notice>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader title="Change history" description="Every revision of this server's configuration." />
        {s.versions.length === 0 ? (
          <p className="px-5 py-4 text-sm text-muted">No changes recorded.</p>
        ) : (
          <ol className="divide-y divide-border">
            {[...s.versions]
              .sort((a, b) => b.revision - a.revision)
              .map((v) => (
                <li key={v.revision} className="flex flex-wrap items-center gap-3 px-5 py-2.5 text-sm">
                  <span className="tabular w-10 text-muted">r{v.revision}</span>
                  <Badge>{v.change.replace(/_/g, " ")}</Badge>
                  <span className="font-mono text-xs text-muted">{v.changed_by}</span>
                  <span className="ml-auto text-xs text-muted">{formatDateTime(v.created_at)}</span>
                </li>
              ))}
          </ol>
        )}
      </Card>

      {editing ? <EditServerDialog server={s} onClose={() => setEditing(false)} onSaved={srv.reload} /> : null}
      {pinning ? <PinDialog server={s} onClose={() => setPinning(false)} onSaved={srv.reload} /> : null}
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending === "revoke" ? `Revoke ${s.display_name}` : `Disable ${s.display_name}`}
        description={
          pending === "revoke"
            ? "Emergency revocation: permanent. Every tool of this server stops immediately and the server can never be enabled again."
            : "Every tool of this server stops until it is enabled again."
        }
        confirmLabel={pending === "revoke" ? "Revoke server" : "Disable server"}
        typedConfirmation={pending === "revoke" ? s.key : undefined}
        reason={{ minLength: 5, maxLength: 500 }}
        onConfirm={async (reason) => {
          if (!pending) return;
          await api(`/api/v1/platform/ai/mcp-servers/${serverId}/${pending}`, { method: "POST", body: { reason } });
          toast({ tone: "success", title: pending === "revoke" ? "Server revoked" : "Server disabled" });
          srv.reload();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </div>
  );
}

function EditServerDialog({ server, onClose, onSaved }: { server: Detail; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [f, setF] = React.useState<ServerForm>(() => serverToForm(server));
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const preview = serverPatch(server, f);
  const set = (k: keyof ServerForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setF((s) => ({ ...s, [k]: e.target.value }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const { patch, errors: local } = serverPatch(server, f);
    setErrors(local);
    if (Object.keys(local).length) return;
    if (!patch) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/mcp-servers/${server.id}`, { method: "PATCH", body: patch });
      toast({ tone: "success", title: preview.identityChanged ? "Saved; the server needs a new review" : "Server saved" });
      onSaved();
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  const input = (k: keyof ServerForm, label: string, opts: { hint?: string; mono?: boolean; identity?: boolean } = {}) => (
    <Field id={`edit-${k}`} label={<>{label}{opts.identity ? <span className="ml-1.5 text-xs font-normal text-warn">identity</span> : null}</>} hint={opts.hint} error={errors[k]}>
      {(p) => <Input {...p} value={f[k]} onChange={set(k)} disabled={busy} className={opts.mono ? "font-mono" : undefined} spellCheck={!opts.mono} />}
    </Field>
  );

  return (
    <Dialog open onClose={onClose} busy={busy} title={`Edit ${server.display_name}`} size="xl">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        {error?.isConflict ? (
          <ConflictNotice
            onReload={() => {
              onSaved();
              onClose();
            }}
          >
            {error.message}
          </ConflictNotice>
        ) : null}
        {preview.identityChanged ? (
          <Notice tone="warn" title="This clears the reviewed pin">
            You are changing the server&apos;s endpoint, hosts, credential reference, service identity or trust level. Saving removes the pin and disables the server until it is reviewed and pinned again.
          </Notice>
        ) : null}
        <div className="grid gap-4 md:grid-cols-2">
          {input("display_name", "Display name")}
          {input("owner", "Owner")}
          {input("endpoint", "Endpoint", { mono: true, identity: true })}
          <Field id="edit-hosts" label={<>Allowed hosts<span className="ml-1.5 text-xs font-normal text-warn">identity</span></>} hint="One per line." error={errors.allowed_hosts}>
            {(p) => <Textarea {...p} value={f.allowed_hosts} onChange={set("allowed_hosts")} disabled={busy} rows={2} className="font-mono" />}
          </Field>
          {input("service_identity", "Service identity", { mono: true, identity: true })}
          <Field id="edit-trust" label={<>Trust level<span className="ml-1.5 text-xs font-normal text-warn">identity</span></>} error={errors.trust_level}>
            {(p) => (
              <Select {...p} value={f.trust_level} onChange={set("trust_level")} disabled={busy}>
                {TRUST_LEVELS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {input("vault_ref", "Credential reference", { mono: true, identity: true, hint: "Leave empty to remove it." })}
          {input("rate_per_minute", "Calls per minute", { hint: "1–6000" })}
          {input("timeout_ms", "Timeout (ms)", { hint: "100–30000" })}
          {input("max_concurrency", "Max concurrency", { hint: "1–64" })}
        </div>
        <Field id="edit-purpose" label="Purpose" error={errors.purpose}>
          {(p) => <Textarea {...p} value={f.purpose} onChange={set("purpose")} disabled={busy} rows={2} />}
        </Field>
        <Field id="edit-description" label="Description" error={errors.description}>
          {(p) => <Textarea {...p} value={f.description} onChange={set("description")} disabled={busy} rows={2} />}
        </Field>
        <FormError message={error && !error.isStepUp && !error.isConflict ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !preview.patch} variant={preview.identityChanged ? "danger" : "primary"}>
            {busy ? "Saving…" : preview.identityChanged ? "Save and clear pin" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function PinDialog({ server, onClose, onSaved }: { server: Detail; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [f, setF] = React.useState<PinForm>({ server_name: server.pinned_server_name ?? server.key, version: "", sha256: "" });
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local = validatePin(f);
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/platform/ai/mcp-servers/${server.id}`, {
        method: "PATCH",
        body: { expected_version: server.row_version, pin: { server_name: f.server_name.trim(), version: f.version.trim(), sha256: f.sha256.trim() } },
      });
      toast({ tone: "success", title: "Pinned", description: "You are recorded as the reviewer." });
      onSaved();
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} busy={busy} title="Pin a reviewed version" description="You are recorded as the reviewer of this exact build." size="md">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        {error?.isConflict ? (
          <ConflictNotice
            onReload={() => {
              onSaved();
              onClose();
            }}
          >
            {error.message}
          </ConflictNotice>
        ) : null}
        {(["server_name", "version", "sha256"] as const).map((k) => (
          <Field key={k} id={`pin-${k}`} label={k === "server_name" ? "Server name" : k === "version" ? "Version" : "SHA-256"} error={errors[k] ?? errors[`pin.${k}`]}>
            {(p) => <Input {...p} value={f[k]} onChange={(e) => setF((s) => ({ ...s, [k]: e.target.value }))} disabled={busy} className="font-mono" spellCheck={false} />}
          </Field>
        ))}
        <FormError message={error && !error.isStepUp && !error.isConflict ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Pinning…" : "Pin"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

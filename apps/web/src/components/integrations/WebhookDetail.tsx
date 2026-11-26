"use client";
/**
 * One webhook endpoint: settings (versioned; a stale edit gets a reload prompt), secret rotation, delete,
 * and the delivery log with per-attempt history and redelivery. Response bodies from receivers are never
 * stored, so only status codes and short error summaries are shown.
 */
import { KeyRound, Pencil, Power, RotateCw, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime, shortId } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  ConflictNotice,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  FormError,
  KeyValues,
  LoadingState,
  Select,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { usePoll } from "@/components/crm/lib/use-poll";
import { Mono, StepUpHint } from "@/components/platform-ops/common";
import { EndpointFields, SecretField } from "./EndpointForm";
import { attemptTone, canRedeliver, deliveryTone, endpointPatch, type EndpointForm, eventLabel, validateEndpointForm, validateSecret } from "./lib/webhooks";
import type { Delivery, DeliveryDetail, Endpoint } from "./lib/types";

export function WebhookDetail({ endpointId }: { endpointId: string }) {
  const router = useRouter();
  const can = useCan();
  const toast = useToast();
  const ep = useApi<Endpoint>(`/api/v1/webhooks/endpoints/${endpointId}`);
  const [editing, setEditing] = React.useState(false);
  const [rotating, setRotating] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [toggling, setToggling] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const canManage = can("integrations:manage");

  if (ep.loading && !ep.data) return <Card><LoadingState label="Loading endpoint" /></Card>;
  if (ep.error && !ep.data) return <Card><ErrorState problem={ep.error.problem} onRetry={ep.reload} /></Card>;
  const e = ep.data;
  if (!e) return null;

  async function toggle(endpoint: Endpoint) {
    setToggling(true);
    setError(null);
    try {
      const next = await api<Endpoint>(`/api/v1/webhooks/endpoints/${endpoint.id}`, {
        method: "PATCH",
        body: { expected_version: endpoint.version, status: endpoint.status === "active" ? "disabled" : "active" },
      });
      ep.setData(() => next);
      toast({ tone: "success", title: next.status === "active" ? "Endpoint enabled" : "Endpoint disabled" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setToggling(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Link href="/app/integrations/webhooks" className="text-sm text-brand hover:underline">
        ← All webhooks
      </Link>
      <Card>
        <CardHeader
          title={e.description || "Webhook endpoint"}
          description={e.url}
          actions={<Badge tone={e.status === "active" ? (e.consecutive_failures > 0 ? "warn" : "ok") : "neutral"}>{e.status}</Badge>}
        />
        <div className="flex flex-col gap-4 px-5 py-4">
          {error?.isConflict ? <ConflictNotice onReload={() => { setError(null); ep.reload(); }} /> : <FormError message={error && !error.isStepUp ? error.message : null} />}
          <KeyValues
            items={[
              { label: "URL", value: <Mono>{e.url}</Mono> },
              { label: "Events", value: e.event_types.map(eventLabel).join(", ") },
              { label: "Failures in a row", value: <span className="tabular">{e.consecutive_failures}</span> },
              { label: "Last delivery", value: formatDateTime(e.last_delivery_at) },
              { label: "Signing secret", value: <span className="text-muted">Stored in the vault{e.secret_version ? ` (version ${e.secret_version})` : ""}; never shown.</span> },
              { label: "Updated", value: formatDateTime(e.updated_at) },
            ]}
          />
        </div>
        {canManage && e.status !== "deleted" ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-5 py-3">
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)} data-testid="edit-webhook">
              <Pencil className="h-4 w-4" aria-hidden /> Edit
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setRotating(true)}>
              <KeyRound className="h-4 w-4" aria-hidden /> Rotate secret
            </Button>
            <Button size="sm" variant="secondary" onClick={() => toggle(e)} disabled={toggling}>
              <Power className="h-4 w-4" aria-hidden /> {e.status === "active" ? "Disable" : "Enable"}
            </Button>
            <Button size="sm" variant="danger" onClick={() => setDeleting(true)} className="ml-auto">
              <Trash2 className="h-4 w-4" aria-hidden /> Delete
            </Button>
            <span className="w-full text-xs text-muted">Editing, enabling and rotating the secret need a recent multi-factor sign-in.</span>
          </div>
        ) : null}
      </Card>

      <Deliveries endpointId={e.id} canManage={canManage} />

      {editing ? <EditDialog endpoint={e} onClose={() => setEditing(false)} onSaved={(n) => ep.setData(() => n)} onReload={ep.reload} /> : null}
      <RotateSecretDialog open={rotating} onClose={() => setRotating(false)} endpoint={e} onRotated={(n) => ep.setData(() => n)} />
      <ConfirmDialog
        open={deleting}
        onClose={() => setDeleting(false)}
        title="Delete webhook endpoint"
        description="Pending deliveries are canceled and the signing secret is deleted. This cannot be undone."
        confirmLabel="Delete endpoint"
        typedConfirmation="delete"
        onConfirm={async () => {
          await api(`/api/v1/webhooks/endpoints/${e.id}`, { method: "DELETE" });
          toast({ tone: "success", title: "Endpoint deleted" });
          router.push("/app/integrations/webhooks");
        }}
      />
    </div>
  );
}

function EditDialog({ endpoint, onClose, onSaved, onReload }: { endpoint: Endpoint; onClose: () => void; onSaved: (e: Endpoint) => void; onReload: () => void }) {
  const toast = useToast();
  const [form, setForm] = React.useState<EndpointForm>({ url: endpoint.url, description: endpoint.description ?? "", event_types: endpoint.event_types, secret: "" });
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    const local = validateEndpointForm(form, { requireSecret: false });
    setErrors(local);
    if (Object.keys(local).length) return;
    const patch = endpointPatch(endpoint, form);
    if (!patch) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await api<Endpoint>(`/api/v1/webhooks/endpoints/${endpoint.id}`, { method: "PATCH", body: patch });
      onSaved(next);
      toast({ tone: "success", title: "Endpoint saved" });
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
    <Dialog open onClose={onClose} busy={busy} title="Edit webhook endpoint" size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        {error?.isConflict ? (
          <ConflictNotice
            onReload={() => {
              onReload();
              onClose();
            }}
          />
        ) : null}
        <EndpointFields value={form} onChange={setForm} errors={errors} disabled={busy} withSecret={false} />
        <FormError message={error && !error.isStepUp && !error.isConflict ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} data-testid="save-webhook">
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RotateSecretDialog({ open, onClose, endpoint, onRotated }: { open: boolean; onClose: () => void; endpoint: Endpoint; onRotated: (e: Endpoint) => void }) {
  const toast = useToast();
  const [secret, setSecret] = React.useState("");
  const [fieldError, setFieldError] = React.useState<string | undefined>();
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setSecret("");
    setFieldError(undefined);
    setError(null);
  }, [open]);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    const local = validateSecret(secret);
    setFieldError(local ?? undefined);
    if (local) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api<Endpoint>(`/api/v1/webhooks/endpoints/${endpoint.id}/rotate-secret`, { method: "POST", body: { secret } });
      setSecret("");
      onRotated(next);
      toast({ tone: "success", title: "Signing secret rotated", description: "Deliveries from now on are signed with the new secret." });
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setFieldError(x.fields.secret);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Rotate signing secret" description="Update your receiver with the new secret right after saving." size="md">
      <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="off" noValidate>
        <StepUpHint />
        <SecretField id="rotate-secret" value={secret} onChange={setSecret} error={fieldError} disabled={busy} />
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Rotating…" : "Rotate secret"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Deliveries({ endpointId, canManage }: { endpointId: string; canManage: boolean }) {
  const [status, setStatus] = React.useState("");
  const [open, setOpen] = React.useState<string | null>(null);
  const list = useCursorList<Delivery>(withQuery(`/api/v1/webhooks/endpoints/${endpointId}/deliveries`, { status }));
  usePoll(list.reload, list.items.some((d) => d.status === "queued" || d.status === "delivering"));

  const columns: Column<Delivery>[] = [
    {
      key: "event",
      header: "Event",
      cell: (d) => (
        <button type="button" onClick={() => setOpen(d.id)} className="text-left text-brand hover:underline">
          {eventLabel(d.event_type)}
          <span className="block font-mono text-xs text-muted">{shortId(d.event_id)}</span>
        </button>
      ),
    },
    { key: "status", header: "Status", cell: (d) => <Badge tone={deliveryTone(d.status)}>{d.status}</Badge> },
    { key: "attempts", header: "Attempts", cell: (d) => <span className="tabular">{d.attempts}</span> },
    { key: "code", header: "Last response", hideOnMobile: true, cell: (d) => (d.last_status_code ? <Mono>HTTP {d.last_status_code}</Mono> : d.last_error ? <span className="text-xs text-danger">{d.last_error}</span> : "—") },
    {
      key: "next",
      header: "Next attempt",
      hideOnMobile: true,
      cell: (d) => (d.status === "queued" || d.status === "delivering" ? <span title={formatDateTime(d.next_attempt_at)}>{relativeTime(d.next_attempt_at)}</span> : "—"),
    },
    { key: "created", header: "Created", hideOnMobile: true, cell: (d) => <span title={formatDateTime(d.created_at)}>{relativeTime(d.created_at)}</span> },
  ];

  return (
    <Card>
      <CardHeader
        title="Delivery log"
        description="Each event is attempted with exponential backoff. Redelivering a failed delivery starts a fresh set of attempts."
        actions={
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Status</span>
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-36">
              <option value="">All</option>
              <option value="queued">Queued</option>
              <option value="delivering">Delivering</option>
              <option value="succeeded">Succeeded</option>
              <option value="failed">Failed</option>
              <option value="canceled">Canceled</option>
            </Select>
          </label>
        }
      />
      <DataTable
        caption="Webhook deliveries"
        columns={columns}
        rows={list.items}
        rowKey={(d) => d.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={status ? "No deliveries with this status" : "No deliveries yet"} description="Deliveries appear when a subscribed event happens." />}
      />
      {open ? <DeliveryDialog endpointId={endpointId} deliveryId={open} canManage={canManage} onClose={() => setOpen(null)} onChanged={list.reload} /> : null}
    </Card>
  );
}

function DeliveryDialog({ endpointId, deliveryId, canManage, onClose, onChanged }: { endpointId: string; deliveryId: string; canManage: boolean; onClose: () => void; onChanged: () => void }) {
  const toast = useToast();
  const d = useApi<DeliveryDetail>(`/api/v1/webhooks/endpoints/${endpointId}/deliveries/${deliveryId}`);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  async function redeliver() {
    setBusy(true);
    setError(null);
    try {
      const next = await api<DeliveryDetail>(`/api/v1/webhooks/endpoints/${endpointId}/deliveries/${deliveryId}/redeliver`, { method: "POST" });
      d.setData(() => next);
      onChanged();
      toast({ tone: "success", title: "Delivery queued again" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const v = d.data;
  return (
    <Dialog
      open
      onClose={onClose}
      busy={busy}
      title="Delivery"
      size="lg"
      footer={
        v && canManage && canRedeliver(v.status) ? (
          <Button onClick={redeliver} disabled={busy} data-testid="redeliver">
            <RotateCw className="h-4 w-4" aria-hidden /> {busy ? "Queuing…" : "Redeliver"}
          </Button>
        ) : undefined
      }
    >
      {d.loading && !v ? (
        <LoadingState label="Loading delivery" />
      ) : d.error && !v ? (
        <ErrorState problem={d.error.problem} onRetry={d.reload} />
      ) : v ? (
        <div className="flex flex-col gap-4">
          <FormError message={error && !error.isStepUp ? error.message : null} />
          <KeyValues
            items={[
              { label: "Event", value: `${eventLabel(v.event_type)} (${v.event_type})` },
              { label: "Event ID", value: <Mono>{v.event_id}</Mono> },
              { label: "Status", value: <Badge tone={deliveryTone(v.status)}>{v.status}</Badge> },
              { label: "Delivered", value: formatDateTime(v.delivered_at) },
            ]}
          />
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="mb-2 text-left text-sm font-medium">Attempts</caption>
              <thead>
                <tr className="text-xs uppercase tracking-wide text-muted">
                  <th scope="col" className="border-b border-border px-3 py-2">#</th>
                  <th scope="col" className="border-b border-border px-3 py-2">Outcome</th>
                  <th scope="col" className="border-b border-border px-3 py-2">Response</th>
                  <th scope="col" className="border-b border-border px-3 py-2">Duration</th>
                  <th scope="col" className="border-b border-border px-3 py-2">When</th>
                </tr>
              </thead>
              <tbody>
                {v.attempt_log.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-center text-muted">
                      No attempts yet.
                    </td>
                  </tr>
                ) : (
                  v.attempt_log.map((a) => (
                    <tr key={a.attempt}>
                      <td className="tabular border-b border-border px-3 py-2">{a.attempt}</td>
                      <td className="border-b border-border px-3 py-2">
                        <Badge tone={attemptTone(a.outcome)}>{a.outcome}</Badge>
                      </td>
                      <td className="border-b border-border px-3 py-2 text-xs">{a.status_code ? `HTTP ${a.status_code}` : a.error ?? "—"}</td>
                      <td className="tabular border-b border-border px-3 py-2">{a.duration_ms !== null ? `${a.duration_ms} ms` : "—"}</td>
                      <td className="border-b border-border px-3 py-2">{formatDateTime(a.created_at)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}

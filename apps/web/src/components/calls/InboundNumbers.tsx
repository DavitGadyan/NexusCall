"use client";
/**
 * Inbound phone numbers (calls:manage, audited). Numbers are entered manually with their provider number
 * SID (ADR-0003). Each number routes to one voice agent; "business hours only" uses the workspace calling
 * window from Settings.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { relativeTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
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
  Field,
  FormError,
  Input,
  StatusBadge,
  useToast,
} from "@/components/ui";
import { AgentSelect, useAgentNames } from "./pickers";
import { type NumberForm, validateNumberForm } from "./number-form";
import type { InboundNumber } from "./types";

export function InboundNumbers() {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<InboundNumber>("/api/v1/inbound-numbers");
  const agentName = useAgentNames();
  const [editing, setEditing] = React.useState<InboundNumber | "new" | null>(null);
  const [deleting, setDeleting] = React.useState<InboundNumber | null>(null);
  const [toggling, setToggling] = React.useState<string | null>(null);
  const manage = can("calls:manage");

  async function toggle(n: InboundNumber) {
    setToggling(n.id);
    try {
      await api(`/api/v1/inbound-numbers/${n.id}`, { method: "PATCH", body: { status: n.status === "active" ? "disabled" : "active", expected_version: n.version } });
      toast({ tone: "success", title: n.status === "active" ? "Number disabled" : "Number enabled" });
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: e.isConflict ? "Changed by someone else — reloaded" : "Could not update the number", description: e.isConflict ? undefined : e.message });
    } finally {
      setToggling(null);
      list.reload();
    }
  }

  const columns: Column<InboundNumber>[] = [
    {
      key: "number",
      header: "Number",
      cell: (n) => (
        <div>
          <span className="font-mono font-medium">{n.e164}</span>
          {n.label ? <span className="block text-xs text-muted">{n.label}</span> : null}
        </div>
      ),
    },
    { key: "agent", header: "Answering agent", cell: (n) => agentName(n.agent_id) },
    {
      key: "hours",
      header: "Hours",
      cell: (n) => (n.business_hours_only ? <Badge tone="warn">Business hours only</Badge> : <Badge>Always</Badge>),
    },
    { key: "handoff", header: "Handoff to", cell: (n) => <span className="font-mono text-xs">{n.handoff_e164 ?? "—"}</span>, hideOnMobile: true },
    { key: "outbound", header: "Outbound caller ID", cell: (n) => (n.use_for_outbound ? "Yes" : "No"), hideOnMobile: true },
    { key: "status", header: "Status", cell: (n) => <StatusBadge status={n.status} /> },
    { key: "updated", header: "Updated", cell: (n) => relativeTime(n.updated_at), hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (n) =>
        manage ? (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => setEditing(n)}>
              Edit
            </Button>
            <Button size="sm" variant="ghost" onClick={() => toggle(n)} disabled={toggling === n.id}>
              {n.status === "active" ? "Disable" : "Enable"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDeleting(n)}>
              Delete
            </Button>
          </div>
        ) : null,
    },
  ];

  return (
    <>
      <Card>
        <CardHeader
          title="Inbound numbers"
          description="Each number is answered by one voice agent. Business-hours numbers follow the calling window in workspace settings."
          actions={
            manage ? (
              <Button size="sm" onClick={() => setEditing("new")} data-testid="add-number">
                Add number
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Inbound numbers"
          columns={columns}
          rows={list.items}
          rowKey={(n) => n.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No phone numbers yet" description="Add a number you own at your telephony provider so callers can reach an AI agent." />}
        />
      </Card>
      <p className="mt-3 text-xs text-muted">
        Business hours are set in{" "}
        <Link href="/admin/settings" className="text-brand hover:underline">
          workspace settings
        </Link>
        .
      </p>
      <NumberDialog
        target={editing}
        onClose={() => setEditing(null)}
        onSaved={(created) => {
          setEditing(null);
          list.reload();
          toast({ tone: "success", title: created ? "Number added" : "Number updated" });
        }}
        onConflict={() => {
          setEditing(null);
          list.reload();
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete this number?"
        description={`Calls to ${deleting?.e164 ?? ""} will no longer reach an agent. You can add it again later.`}
        confirmLabel="Delete number"
        typedConfirmation={deleting?.e164}
        onConfirm={async () => {
          if (!deleting) return;
          await api(`/api/v1/inbound-numbers/${deleting.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Number deleted" });
        }}
      />
    </>
  );
}

function NumberDialog({
  target,
  onClose,
  onSaved,
  onConflict,
}: {
  target: InboundNumber | "new" | null;
  onClose: () => void;
  onSaved: (created: boolean) => void;
  onConflict: () => void;
}) {
  const creating = target === "new";
  const existing = target !== "new" && target !== null ? target : null;
  const [form, setForm] = React.useState<NumberForm>(empty());
  const [local, setLocal] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setLocal({});
    setError(null);
    if (existing) {
      setForm({
        e164: existing.e164,
        label: existing.label,
        agent_id: existing.agent_id,
        business_hours_only: existing.business_hours_only,
        handoff_e164: existing.handoff_e164 ?? "",
        provider_number_sid: existing.provider_number_sid ?? "",
        use_for_outbound: existing.use_for_outbound,
      });
    } else setForm(empty());
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof NumberForm>(k: K, v: NumberForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const fieldError = (k: string) => local[k] ?? error?.fields[k];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const v = validateNumberForm(form, creating);
    setLocal(v);
    if (Object.keys(v).length) return;
    setBusy(true);
    setError(null);
    try {
      if (creating) {
        await api("/api/v1/inbound-numbers", {
          method: "POST",
          body: {
            e164: form.e164.trim(),
            label: form.label.trim(),
            agent_id: form.agent_id,
            business_hours_only: form.business_hours_only,
            handoff_e164: form.handoff_e164.trim() || null,
            provider_number_sid: form.provider_number_sid.trim() || null,
            use_for_outbound: form.use_for_outbound,
          },
        });
      } else if (existing) {
        await api(`/api/v1/inbound-numbers/${existing.id}`, {
          method: "PATCH",
          body: {
            expected_version: existing.version,
            label: form.label.trim(),
            agent_id: form.agent_id,
            business_hours_only: form.business_hours_only,
            handoff_e164: form.handoff_e164.trim() || null,
            use_for_outbound: form.use_for_outbound,
          },
        });
      }
      onSaved(creating);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const conflictOnCreate = creating && error?.isConflict;
  return (
    <Dialog open={target !== null} onClose={onClose} busy={busy} title={creating ? "Add a phone number" : `Edit ${existing?.e164 ?? ""}`} size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        {error?.isConflict && !creating ? <ConflictNotice onReload={onConflict} /> : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="num-e164" label="Phone number (E.164)" required error={fieldError("e164")} hint={creating ? undefined : "The number itself cannot be changed."}>
            {(p) => <Input {...p} value={form.e164} onChange={(e) => set("e164", e.target.value)} disabled={!creating} placeholder="+14155550123" inputMode="tel" autoComplete="off" />}
          </Field>
          <Field id="num-label" label="Label" error={fieldError("label")}>
            {(p) => <Input {...p} value={form.label} maxLength={120} onChange={(e) => set("label", e.target.value)} placeholder="Main line" />}
          </Field>
        </div>
        <Field id="num-agent" label="Answering voice agent" required error={fieldError("agent_id")}>
          {(p) => <AgentSelect kind="voice" control={p} value={form.agent_id} onChange={(v) => set("agent_id", v)} />}
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="num-handoff" label="Handoff number (optional)" hint="Where the AI transfers callers who ask for a person." error={fieldError("handoff_e164")}>
            {(p) => <Input {...p} value={form.handoff_e164} onChange={(e) => set("handoff_e164", e.target.value)} placeholder="+14155550199" inputMode="tel" autoComplete="off" />}
          </Field>
          <Field id="num-sid" label="Provider number SID" hint={creating ? "From your Twilio console (PN…)." : "Set when the number was added."} error={fieldError("provider_number_sid")}>
            {(p) => <Input {...p} value={form.provider_number_sid} onChange={(e) => set("provider_number_sid", e.target.value)} disabled={!creating} className="font-mono" autoComplete="off" spellCheck={false} />}
          </Field>
        </div>
        <Checkbox
          checked={form.business_hours_only}
          onChange={(e) => set("business_hours_only", e.target.checked)}
          label="Answer only during business hours"
          description="Outside the workspace calling window, callers hear a closed message."
        />
        <Checkbox checked={form.use_for_outbound} onChange={(e) => set("use_for_outbound", e.target.checked)} label="Use as caller ID for outbound calls" />
        <FormError message={conflictOnCreate ? "This phone number is already registered." : error && !error.isConflict && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : creating ? "Add number" : "Save changes"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function empty(): NumberForm {
  return { e164: "", label: "", agent_id: "", business_hours_only: false, handoff_e164: "", provider_number_sid: "", use_for_outbound: true };
}

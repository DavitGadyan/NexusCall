"use client";
/**
 * Live calls board (build.md §8.2 "Call operations dashboard"). Driven by `call.updated` hints on the live
 * event stream; polls every 5 s while the stream is down. Hang-up is re-authorised and audited server-side.
 */
import { PhoneIncoming, PhoneOff, PhoneOutgoing, Radio } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import type { Page } from "@/lib/types";
import { useCan } from "@/components/shell/session-context";
import { Badge, Button, Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Field, FormError, LoadingState, useToast } from "@/components/ui";
import { AgentSelect, ContactPicker, useAgentNames } from "./pickers";
import { callStateTone, formatDuration, liveElapsedSeconds } from "./shared";
import type { Call, Contact, CreateCallOut, HangupOut } from "./types";
import { useIdempotencyKey, useLiveRefresh, useNow } from "./use-live";

const COLUMNS: { state: string; title: string; hint: string }[] = [
  { state: "initiating", title: "Dialing", hint: "Placing the call" },
  { state: "ringing", title: "Ringing", hint: "Waiting for an answer" },
  { state: "connected", title: "Connected", hint: "Answered, AI greeting" },
  { state: "in_progress", title: "In conversation", hint: "AI or person talking" },
];

export function LiveCalls() {
  const can = useCan();
  const toast = useToast();
  const live = useApi<Page<Call>>("/api/v1/calls/live");
  const status = useLiveRefresh(live.reload, { events: ["call.updated"], pollMs: 5000, slowPollMs: 60_000 });
  const now = useNow(1000);
  const agentName = useAgentNames();
  const [hangingUp, setHangingUp] = React.useState<Call | null>(null);
  const [testOpen, setTestOpen] = React.useState(false);
  const calls = live.data?.items ?? [];
  const canManage = can("calls:manage");

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="inline-flex items-center gap-2 text-sm" aria-live="polite">
          <Radio className={status === "open" ? "h-4 w-4 text-ok" : "h-4 w-4 text-warn"} aria-hidden />
          {status === "open" ? "Live updates on" : "Live updates unavailable — refreshing every 5 seconds"}
          <span className="text-muted">· {calls.length} active</span>
        </p>
        {canManage ? (
          <Button size="sm" variant="secondary" onClick={() => setTestOpen(true)} data-testid="open-test-call">
            Place a test call
          </Button>
        ) : null}
      </div>
      {live.loading && !live.data ? (
        <Card>
          <LoadingState label="Loading live calls" />
        </Card>
      ) : live.error && !live.data ? (
        <Card>
          <ErrorState problem={live.error.problem} onRetry={live.reload} />
        </Card>
      ) : calls.length === 0 ? (
        <Card>
          <EmptyState title="No calls in progress" description="Calls appear here as soon as dialing starts, and leave when they end." />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4" data-testid="live-board">
          {COLUMNS.map((col) => {
            const items = calls.filter((c) => c.state === col.state);
            return (
              <section key={col.state} aria-labelledby={`col-${col.state}`} className="flex flex-col gap-2">
                <header className="flex items-baseline justify-between px-1">
                  <h2 id={`col-${col.state}`} className="text-sm font-semibold">
                    {col.title} <span className="tabular text-muted">({items.length})</span>
                  </h2>
                  <span className="text-xs text-muted">{col.hint}</span>
                </header>
                {items.length === 0 ? <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-xs text-muted">None</p> : null}
                <ul className="flex flex-col gap-2">
                  {items.map((c) => (
                    <li key={c.id}>
                      <LiveCallCard call={c} now={now} agentName={agentName(c.agent_id)} canManage={canManage} onHangup={() => setHangingUp(c)} />
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
      {live.error && live.data ? <p className="mt-3 text-xs text-danger">Last refresh failed: {live.error.message}</p> : null}
      <ConfirmDialog
        open={hangingUp !== null}
        onClose={() => setHangingUp(null)}
        title="End this call now?"
        description="The other party hears the call end immediately. Your name and the time are recorded in the audit log."
        confirmLabel="Hang up"
        onConfirm={async () => {
          if (!hangingUp) return;
          const out = await api<HangupOut>(`/api/v1/calls/${hangingUp.id}/hangup`, { method: "POST" });
          toast(
            out.hangup === "not_live"
              ? { tone: "info", title: "The call had already ended" }
              : out.hangup === "canceled"
                ? { tone: "success", title: "Call canceled before it connected" }
                : { tone: "success", title: "Hang-up requested", description: "The board updates when the provider confirms." },
          );
          live.reload();
        }}
      />
      <TestCallDialog open={testOpen} onClose={() => setTestOpen(false)} onPlaced={live.reload} />
    </>
  );
}

function LiveCallCard({ call, now, agentName, canManage, onHangup }: { call: Call; now: number; agentName: string; canManage: boolean; onHangup: () => void }) {
  const inbound = call.direction === "inbound";
  const party = inbound ? call.from_masked : call.to_masked;
  const Icon = inbound ? PhoneIncoming : PhoneOutgoing;
  return (
    <Card className="flex flex-col gap-2 px-3 py-3 text-sm" data-testid="live-call">
      <div className="flex items-start justify-between gap-2">
        <Link href={`/app/calls/${call.id}`} className="inline-flex min-w-0 items-center gap-1.5 font-medium text-brand hover:underline">
          <Icon className="h-4 w-4 shrink-0" aria-hidden />
          <span className="sr-only">{inbound ? "Inbound from" : "Outbound to"}</span>
          <span className="truncate font-mono">{party ?? "Unknown number"}</span>
        </Link>
        <span className="tabular shrink-0 font-mono text-xs" title={call.answered_at ? "Talk time" : "Time since dialing started"}>
          {formatDuration(liveElapsedSeconds(call, now))}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Badge tone={callStateTone(call.state)}>{call.state.replace(/_/g, " ")}</Badge>
        {call.is_test ? <Badge tone="brand">test</Badge> : null}
        {call.handoff_state ? <Badge tone="warn">handoff {call.handoff_state.replace(/_/g, " ")}</Badge> : null}
      </div>
      <p className="truncate text-xs text-muted">Agent: {agentName}</p>
      {canManage ? (
        <div className="flex justify-end">
          <Button size="sm" variant="danger" onClick={onHangup} aria-label={`Hang up call ${party ?? call.id}`}>
            <PhoneOff className="h-3.5 w-3.5" aria-hidden /> Hang up
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

/** Test call to an existing contact; all pre-dial gates (consent, DNC, quiet hours) still apply server-side. */
export function TestCallDialog({ open, onClose, onPlaced }: { open: boolean; onClose: () => void; onPlaced: () => void }) {
  const toast = useToast();
  const key = useIdempotencyKey(open);
  const [agentId, setAgentId] = React.useState("");
  const [contact, setContact] = React.useState<Contact | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (open) {
      setAgentId("");
      setContact(null);
      setError(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!contact || !agentId) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<CreateCallOut>("/api/v1/calls/test", { method: "POST", body: { agent_id: agentId, contact_id: contact.id }, idempotencyKey: key || true });
      toast({ tone: "success", title: "Test call queued", description: `State: ${out.state.replace(/_/g, " ")}` });
      onPlaced();
      onClose();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const formError = error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null;
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Place a test call" description="Calls an existing contact with the selected voice agent. Consent, do-not-call and calling-window rules still apply.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="test-agent" label="Voice agent" required error={error?.fields.agent_id}>
          {(p) => <AgentSelect kind="voice" control={p} value={agentId} onChange={setAgentId} />}
        </Field>
        <Field id="test-contact" label="Contact" required error={error?.fields.contact_id}>
          {(p) => <ContactPicker control={p} value={contact} onChange={setContact} />}
        </Field>
        <FormError message={formError} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !agentId || !contact}>
            {busy ? "Placing…" : "Place test call"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

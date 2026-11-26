"use client";
/**
 * Booking dialogs: details, create / reschedule (pick a free slot or enter a time), cancel and outcome.
 * Writes carry an Idempotency-Key that stays the same while the dialog is open. A 409 on create or
 * reschedule usually means the slot was just taken (database exclusion constraint): the slot list reloads.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
import { Badge, Button, ConflictNotice, Dialog, Field, FormError, Input, KeyValues, Notice, Select, StatusBadge, useToast } from "@/components/ui";
import { ContactPicker } from "@/components/calls/pickers";
import type { Contact } from "@/components/calls/types";
import { useIdempotencyKey } from "@/components/calls/use-live";
import { addDays, classifyBookingConflict, formatRange, formatTime, fromDateInput, fromLocalInput, startOfDay, toDateInput, toLocalInput } from "./calendar";
import { type Availability, type Booking, CANCEL_REASONS, type Resource } from "./types";

export function useResources() {
  return useApi<{ items: Resource[] }>("/api/v1/bookings/resources");
}

export function statusTone(status: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (status === "confirmed") return "ok";
  if (status === "tentative") return "warn";
  if (status === "completed") return "brand";
  if (status === "no_show") return "danger";
  return "neutral";
}

const DURATIONS = [15, 30, 45, 60, 90, 120];

export function BookingDetailDialog({
  booking,
  resources,
  onClose,
  onChanged,
}: {
  booking: Booking | null;
  resources: Resource[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const can = useCan();
  const [mode, setMode] = React.useState<"view" | "reschedule" | "cancel">("view");
  React.useEffect(() => setMode("view"), [booking?.id]);
  if (!booking) return null;
  const resource = resources.find((r) => r.id === booking.resource_id);
  const manage = can("bookings:manage");
  const open = booking.status === "tentative" || booking.status === "confirmed";
  const started = new Date(booking.start).getTime() <= Date.now();

  if (mode === "reschedule")
    return (
      <BookingFormDialog
        open
        mode="reschedule"
        booking={booking}
        resources={resources}
        onClose={() => setMode("view")}
        onSaved={() => {
          onChanged();
          onClose();
        }}
      />
    );
  if (mode === "cancel")
    return (
      <CancelDialog
        booking={booking}
        onClose={() => setMode("view")}
        onDone={() => {
          onChanged();
          onClose();
        }}
      />
    );
  return (
    <Dialog
      open
      onClose={onClose}
      title={formatRange(booking.start, booking.end)}
      description={resource ? `${resource.name} · times shown in your time zone (resource: ${resource.timezone})` : undefined}
      footer={
        manage && open ? (
          <>
            {booking.status === "confirmed" && started ? <OutcomeButtons booking={booking} onDone={() => { onChanged(); onClose(); }} /> : null}
            <Button variant="secondary" onClick={() => setMode("cancel")}>
              Cancel booking
            </Button>
            <Button onClick={() => setMode("reschedule")}>Reschedule</Button>
          </>
        ) : (
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
        )
      }
    >
      <KeyValues
        items={[
          { label: "Status", value: <Badge tone={statusTone(booking.status)}>{booking.status.replace(/_/g, " ")}</Badge> },
          { label: "Contact", value: <Link className="text-brand hover:underline" href={`/app/crm/contacts/${booking.contact_id}`}>Open contact</Link> },
          { label: "Booked via", value: booking.source_type.replace(/_/g, " ") },
          ...(booking.hold_expires_at && booking.status === "tentative" ? [{ label: "Hold expires", value: `${formatDateTime(booking.hold_expires_at)} (${relativeTime(booking.hold_expires_at)})` }] : []),
          ...(booking.cancel_reason ? [{ label: "Cancel reason", value: booking.cancel_reason.replace(/_/g, " ") }] : []),
          { label: "Calendar sync", value: booking.calendar_sync_state ? <StatusBadge status={booking.calendar_sync_state} /> : "Not synced" },
          { label: "Last change", value: relativeTime(booking.updated_at) },
        ]}
      />
    </Dialog>
  );
}

function OutcomeButtons({ booking, onDone }: { booking: Booking; onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  async function record(status: "completed" | "no_show") {
    setBusy(true);
    try {
      await api(`/api/v1/bookings/${booking.id}/outcome`, { method: "POST", body: { status, expected_version: booking.version } });
      toast({ tone: "success", title: status === "completed" ? "Marked as completed" : "Marked as no-show" });
      onDone();
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Could not record the outcome", description: e.message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Button variant="ghost" disabled={busy} onClick={() => record("no_show")}>
        No-show
      </Button>
      <Button variant="secondary" disabled={busy} onClick={() => record("completed")}>
        Completed
      </Button>
    </>
  );
}

function CancelDialog({ booking, onClose, onDone }: { booking: Booking; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const key = useIdempotencyKey(true);
  const [reason, setReason] = React.useState<(typeof CANCEL_REASONS)[number]>("requested");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/bookings/${booking.id}/cancel`, { method: "POST", body: { reason, expected_version: booking.version }, idempotencyKey: key || true });
      toast({ tone: "success", title: "Booking canceled", description: "The contact's calendar invitation is updated if calendar sync is on." });
      onDone();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={onClose} busy={busy} title="Cancel this booking?" description={formatRange(booking.start, booking.end)} size="sm">
      <form onSubmit={submit} className="flex flex-col gap-4">
        {error?.isConflict ? <ConflictNotice onReload={onDone} /> : null}
        <Field id="cancel-reason" label="Reason">
          {(p) => (
            <Select {...p} value={reason} onChange={(e) => setReason(e.target.value as (typeof CANCEL_REASONS)[number])}>
              {CANCEL_REASONS.map((r) => (
                <option key={r} value={r}>
                  {r.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <FormError message={error && !error.isConflict && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Keep booking
          </Button>
          <Button type="submit" variant="danger" disabled={busy}>
            {busy ? "Canceling…" : "Cancel booking"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** Create (mode "create") or move an existing booking (mode "reschedule"). */
export function BookingFormDialog({
  open,
  mode,
  booking,
  resources,
  initialStart,
  onClose,
  onSaved,
}: {
  open: boolean;
  mode: "create" | "reschedule";
  booking?: Booking;
  resources: Resource[];
  initialStart?: Date;
  onClose: () => void;
  onSaved: (b: Booking) => void;
}) {
  const toast = useToast();
  const key = useIdempotencyKey(open);
  const active = resources.filter((r) => r.status === "active");
  const [contact, setContact] = React.useState<Contact | null>(null);
  const [resourceId, setResourceId] = React.useState("");
  const [day, setDay] = React.useState(toDateInput(new Date()));
  const [manual, setManual] = React.useState(false);
  const [start, setStart] = React.useState("");
  const [duration, setDuration] = React.useState(30);
  const [slot, setSlot] = React.useState<{ start: string; end: string } | null>(null);
  const [status, setStatus] = React.useState<"confirmed" | "tentative">("confirmed");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setError(null);
    setNotice(null);
    setSlot(null);
    setContact(null);
    setResourceId(booking?.resource_id ?? (active.length === 1 ? active[0]!.id : ""));
    const base = initialStart ?? (booking ? new Date(booking.start) : new Date());
    setDay(toDateInput(base));
    setStart(toLocalInput(initialStart ?? (booking ? new Date(booking.start) : base)));
    setDuration(booking ? Math.max(5, Math.round((new Date(booking.end).getTime() - new Date(booking.start).getTime()) / 60_000)) : 30);
    setManual(Boolean(initialStart));
    setStatus("confirmed");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const dayDate = fromDateInput(day);
  const availPath =
    open && !manual && dayDate && resourceId
      ? withQuery("/api/v1/availability", { from: startOfDay(dayDate).toISOString(), to: addDays(startOfDay(dayDate), 1).toISOString(), resource_id: resourceId })
      : null;
  const avail = useApi<Availability>(availPath);
  const slots = avail.data?.slots ?? [];

  function chosen(): { start: string; end: string } | null {
    if (!manual) return slot;
    const s = fromLocalInput(start);
    if (!s) return null;
    return { start: s, end: new Date(new Date(s).getTime() + duration * 60_000).toISOString() };
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const when = chosen();
    if (!when || !resourceId || (mode === "create" && !contact)) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const out =
        mode === "create"
          ? await api<Booking>("/api/v1/bookings", {
              method: "POST",
              body: { contact_id: contact!.id, resource_id: resourceId, start: when.start, end: when.end, status },
              idempotencyKey: key || true,
            })
          : await api<Booking>(`/api/v1/bookings/${booking!.id}/reschedule`, {
              method: "POST",
              body: { start: when.start, end: when.end, resource_id: resourceId, expected_version: booking!.version },
              idempotencyKey: key || true,
            });
      toast({ tone: "success", title: mode === "create" ? "Booking created" : "Booking moved", description: formatRange(out.start, out.end) });
      onSaved(out);
    } catch (err) {
      const apiErr = toApiError(err);
      const kind = classifyBookingConflict(apiErr);
      if (kind === "slot_taken") {
        setNotice("That time was just taken by another booking. Pick a different slot.");
        setSlot(null);
        avail.reload();
      } else setError(apiErr);
    } finally {
      setBusy(false);
    }
  }

  const when = chosen();
  const stale = error ? classifyBookingConflict(error) === "stale" : false;
  const formError = error && !stale && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null;
  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={busy}
      size="lg"
      title={mode === "create" ? "New booking" : "Reschedule booking"}
      description={mode === "reschedule" && booking ? `Currently ${formatRange(booking.start, booking.end)}` : "Times are in your time zone."}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {stale ? <ConflictNotice onReload={onClose}>This booking changed since you opened it. Close, then open it again to see the latest version.</ConflictNotice> : null}
        {notice ? (
          <Notice tone="danger" title="Slot taken">
            {notice}
          </Notice>
        ) : null}
        {mode === "create" ? (
          <Field id="bk-contact" label="Contact" required error={error?.fields.contact_id}>
            {(p) => <ContactPicker control={p} value={contact} onChange={setContact} />}
          </Field>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="bk-resource" label="Resource" required error={error?.fields.resource_id}>
            {(p) => (
              <Select {...p} value={resourceId} onChange={(e) => { setResourceId(e.target.value); setSlot(null); }}>
                <option value="">Select a resource</option>
                {active.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name} ({r.timezone})
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {mode === "create" ? (
            <Field id="bk-status" label="Status" hint="A tentative booking is held for a limited time, then released.">
              {(p) => (
                <Select {...p} value={status} onChange={(e) => setStatus(e.target.value as "confirmed" | "tentative")}>
                  <option value="confirmed">Confirmed</option>
                  <option value="tentative">Tentative (hold)</option>
                </Select>
              )}
            </Field>
          ) : null}
        </div>
        <div className="flex gap-4 text-sm" role="radiogroup" aria-label="How to choose the time">
          <label className="inline-flex items-center gap-2">
            <input type="radio" name="bk-mode" checked={!manual} onChange={() => setManual(false)} /> Pick a free slot
          </label>
          <label className="inline-flex items-center gap-2">
            <input type="radio" name="bk-mode" checked={manual} onChange={() => setManual(true)} /> Enter a time
          </label>
        </div>
        {manual ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id="bk-start" label="Start" required error={error?.fields.start}>
              {(p) => <Input {...p} type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />}
            </Field>
            <Field id="bk-duration" label="Duration" error={error?.fields.end} hint="At most 8 hours.">
              {(p) => (
                <Select {...p} value={duration} onChange={(e) => setDuration(Number(e.target.value))}>
                  {[...new Set([...DURATIONS, duration])].sort((a, b) => a - b).map((d) => (
                    <option key={d} value={d}>
                      {d} minutes
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <Field id="bk-day" label="Day">
              {(p) => <Input {...p} type="date" value={day} onChange={(e) => { setDay(e.target.value); setSlot(null); }} className="max-w-xs" />}
            </Field>
            {!resourceId ? (
              <p className="text-sm text-muted">Choose a resource to see free slots.</p>
            ) : avail.loading ? (
              <p className="text-sm text-muted">Loading free slots…</p>
            ) : avail.error ? (
              <p className="text-sm text-danger">{avail.error.message}</p>
            ) : slots.length === 0 ? (
              <p className="text-sm text-muted">No free slots on this day. Try another day, or enter a time.</p>
            ) : (
              <div role="listbox" aria-label="Free slots" className="flex flex-wrap gap-1.5" data-testid="slots">
                {slots.map((s) => {
                  const selected = slot?.start === s.start;
                  return (
                    <button
                      key={s.start}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      onClick={() => setSlot({ start: s.start, end: s.end })}
                      className={`rounded-md border px-2.5 py-1 text-sm tabular ${selected ? "border-brand bg-brand text-brand-fg" : "border-border hover:bg-surface-2"}`}
                    >
                      {formatTime(s.start)}
                    </button>
                  );
                })}
              </div>
            )}
            {avail.data && avail.data.calendar_unavailable.length > 0 ? (
              <Notice tone="warn">The connected calendar could not be checked just now, so these slots may include times that are busy there.</Notice>
            ) : null}
          </div>
        )}
        {when ? <p className="text-sm">Selected: <span className="font-medium">{formatRange(when.start, when.end)}</span></p> : null}
        <FormError message={formError} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !when || !resourceId || (mode === "create" && !contact)} data-testid="save-booking">
            {busy ? "Saving…" : mode === "create" ? "Create booking" : "Move booking"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

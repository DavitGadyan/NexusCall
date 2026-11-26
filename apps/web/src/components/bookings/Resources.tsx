"use client";
/**
 * Bookable resources (staff or calendars) and their weekly availability rules. Rules are replaced as a
 * whole with an optimistic lock on the resource version (409 -> reload prompt).
 */
import { Plus, Trash2 } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { relativeTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
import {
  Button,
  Card,
  CardHeader,
  type Column,
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
  useToast,
} from "@/components/ui";
import { useResources } from "./BookingDialogs";
import { trimSeconds, validateRules, WEEKDAYS } from "./calendar";
import type { Resource, Rule, RulesOut } from "./types";

export function Resources() {
  const can = useCan();
  const toast = useToast();
  const list = useResources();
  const [editing, setEditing] = React.useState<Resource | "new" | null>(null);
  const [rulesFor, setRulesFor] = React.useState<Resource | null>(null);
  const manage = can("bookings:manage");
  const items = list.data?.items ?? [];

  const columns: Column<Resource>[] = [
    {
      key: "name",
      header: "Name",
      cell: (r) => (
        <div>
          <span className="font-medium">{r.name}</span>
          <span className="block text-xs text-muted">{r.kind === "staff" ? "Staff member" : "Calendar"}</span>
        </div>
      ),
    },
    { key: "tz", header: "Time zone", cell: (r) => r.timezone },
    { key: "cal", header: "External calendar", cell: (r) => (r.calendar_provider ? `${r.calendar_provider.replace(/_/g, " ")} · ${r.calendar_id ?? ""}` : "—"), hideOnMobile: true },
    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status === "inactive" ? "disabled" : r.status} /> },
    { key: "updated", header: "Updated", cell: (r) => relativeTime(r.updated_at), hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (r) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="secondary" onClick={() => setRulesFor(r)}>
            {manage ? "Edit hours" : "View hours"}
          </Button>
          {manage ? (
            <Button size="sm" variant="ghost" onClick={() => setEditing(r)}>
              Edit
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <>
      <Card>
        <CardHeader
          title="Resources"
          description="People or calendars that can be booked. Free slots come from each resource's weekly hours minus existing bookings."
          actions={
            manage ? (
              <Button size="sm" onClick={() => setEditing("new")}>
                Add resource
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Resources"
          columns={columns}
          rows={items}
          rowKey={(r) => r.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={<EmptyState title="No resources yet" description="Add the first staff member or calendar that customers can book." />}
        />
      </Card>
      <ResourceDialog
        target={editing}
        onClose={() => setEditing(null)}
        onSaved={(created) => {
          setEditing(null);
          list.reload();
          toast({ tone: "success", title: created ? "Resource added" : "Resource updated" });
        }}
        onConflict={() => {
          setEditing(null);
          list.reload();
        }}
      />
      {rulesFor ? (
        <RulesDialog
          resource={rulesFor}
          readOnly={!manage}
          onClose={() => setRulesFor(null)}
          onSaved={() => {
            setRulesFor(null);
            list.reload();
            toast({ tone: "success", title: "Weekly hours saved" });
          }}
        />
      ) : null}
    </>
  );
}

function ResourceDialog({ target, onClose, onSaved, onConflict }: { target: Resource | "new" | null; onClose: () => void; onSaved: (created: boolean) => void; onConflict: () => void }) {
  const creating = target === "new";
  const existing = target && target !== "new" ? target : null;
  const [name, setName] = React.useState("");
  const [kind, setKind] = React.useState<"staff" | "calendar">("staff");
  const [timezone, setTimezone] = React.useState("UTC");
  const [status, setStatus] = React.useState<"active" | "inactive">("active");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    setError(null);
    setName(existing?.name ?? "");
    setKind(existing?.kind === "calendar" ? "calendar" : "staff");
    setTimezone(existing?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC");
    setStatus(existing?.status === "inactive" ? "inactive" : "active");
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (creating) await api("/api/v1/bookings/resources", { method: "POST", body: { name: name.trim(), kind, timezone: timezone.trim() } });
      else if (existing) await api(`/api/v1/bookings/resources/${existing.id}`, { method: "PATCH", body: { expected_version: existing.version, name: name.trim(), timezone: timezone.trim(), status } });
      onSaved(creating);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const nameError = error?.fields.name ?? (name.trim().length > 0 && name.trim().length < 2 ? "At least 2 characters." : undefined);
  return (
    <Dialog open={target !== null} onClose={onClose} busy={busy} title={creating ? "Add a resource" : `Edit ${existing?.name ?? ""}`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        {error?.isConflict && !creating && /changed by someone else/i.test(error.message) ? <ConflictNotice onReload={onConflict} /> : null}
        <Field id="res-name" label="Name" required error={nameError}>
          {(p) => <Input {...p} value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="Dr. Ana Lopez" />}
        </Field>
        {creating ? (
          <Field id="res-kind" label="Kind">
            {(p) => (
              <Select {...p} value={kind} onChange={(e) => setKind(e.target.value as "staff" | "calendar")}>
                <option value="staff">Staff member</option>
                <option value="calendar">Calendar (room, equipment, shared calendar)</option>
              </Select>
            )}
          </Field>
        ) : null}
        <Field id="res-tz" label="Time zone" hint="IANA name, e.g. America/New_York. Weekly hours are in this zone." error={error?.fields.timezone ?? (error?.message === "Unknown timezone" ? "Unknown time zone." : undefined)}>
          {(p) => <Input {...p} value={timezone} onChange={(e) => setTimezone(e.target.value)} spellCheck={false} />}
        </Field>
        {!creating ? (
          <Field id="res-status" label="Status" hint="Inactive resources take no new bookings; existing ones stay.">
            {(p) => (
              <Select {...p} value={status} onChange={(e) => setStatus(e.target.value as "active" | "inactive")}>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
              </Select>
            )}
          </Field>
        ) : null}
        <p className="text-xs text-muted">Calendar sync with Google Calendar is configured from the provider connection under Integrations.</p>
        <FormError
          message={
            error && !error.isStepUp && Object.keys(error.fields).length === 0 && !(error.isConflict && /changed by someone else/i.test(error.message)) && error.message !== "Unknown timezone" ? error.message : null
          }
        />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || name.trim().length < 2}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

const NEW_RULE: Omit<Rule, "weekday"> = { start_time: "09:00", end_time: "17:00", slot_minutes: 30, buffer_before_minutes: 0, buffer_after_minutes: 0, min_notice_minutes: 60 };

function RulesDialog({ resource, readOnly, onClose, onSaved }: { resource: Resource; readOnly: boolean; onClose: () => void; onSaved: () => void }) {
  const remote = useApi<RulesOut>(`/api/v1/bookings/resources/${resource.id}/rules`);
  const [rules, setRules] = React.useState<Rule[] | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [local, setLocal] = React.useState<Record<string, string>>({});

  React.useEffect(() => {
    if (remote.data) setRules(remote.data.rules.map((r) => ({ ...r, start_time: trimSeconds(r.start_time), end_time: trimSeconds(r.end_time) })));
  }, [remote.data]);

  const update = (i: number, patch: Partial<Rule>) => setRules((rs) => (rs ? rs.map((r, k) => (k === i ? { ...r, ...patch } : r)) : rs));

  async function save() {
    if (!rules || !remote.data) return;
    const v = validateRules(rules);
    setLocal(v);
    if (Object.keys(v).length) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/api/v1/bookings/resources/${resource.id}/rules`, {
        method: "PUT",
        body: {
          expected_version: remote.data.resource_version,
          rules: rules.map((r) => ({
            weekday: r.weekday,
            start_time: r.start_time,
            end_time: r.end_time,
            slot_minutes: r.slot_minutes,
            buffer_before_minutes: r.buffer_before_minutes,
            buffer_after_minutes: r.buffer_after_minutes,
            min_notice_minutes: r.min_notice_minutes,
          })),
        },
      });
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const err = (i: number, f: string) => local[`rules.${i}.${f}`] ?? error?.fields[`rules.${i}.${f}`];
  const sorted = rules ? rules.map((r, i) => ({ r, i })).sort((a, b) => a.r.weekday - b.r.weekday || a.r.start_time.localeCompare(b.r.start_time)) : [];
  return (
    <Dialog
      open
      onClose={onClose}
      busy={busy}
      size="xl"
      title={`Weekly hours — ${resource.name}`}
      description={`Times are in ${resource.timezone}. Slots start every "slot length" within each window.`}
      footer={
        readOnly ? (
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={save} disabled={busy || !rules}>
              {busy ? "Saving…" : "Save hours"}
            </Button>
          </>
        )
      }
    >
      {remote.loading && !rules ? (
        <LoadingState label="Loading hours" />
      ) : remote.error ? (
        <ErrorState problem={remote.error.problem} onRetry={remote.reload} />
      ) : rules ? (
        <div className="flex flex-col gap-3">
          {error?.isConflict ? (
            <ConflictNotice
              onReload={() => {
                setError(null);
                remote.reload();
              }}
            />
          ) : null}
          <FormError message={error && !error.isConflict && !error.isStepUp ? error.message : null} />
          {rules.length === 0 ? <Notice tone="info">No weekly hours yet, so this resource has no free slots.</Notice> : null}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <caption className="sr-only">Weekly availability windows</caption>
              <thead>
                <tr className="text-xs uppercase tracking-wide text-muted">
                  <th className="px-2 py-1.5">Day</th>
                  <th className="px-2 py-1.5">From</th>
                  <th className="px-2 py-1.5">To</th>
                  <th className="px-2 py-1.5">Slot length (min)</th>
                  <th className="px-2 py-1.5">Buffer before / after (min)</th>
                  <th className="px-2 py-1.5">Min. notice (min)</th>
                  <th className="px-2 py-1.5">
                    <span className="sr-only">Remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map(({ r, i }) => (
                  <tr key={i} className="border-t border-border align-top">
                    <td className="px-2 py-1.5">
                      <Select aria-label="Weekday" value={r.weekday} disabled={readOnly} onChange={(e) => update(i, { weekday: Number(e.target.value) })} className="w-36">
                        {WEEKDAYS.map((d, k) => (
                          <option key={d} value={k}>
                            {d}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <RuleInput label="From" type="time" value={r.start_time} error={err(i, "start_time")} disabled={readOnly} onChange={(v) => update(i, { start_time: v })} />
                    <RuleInput label="To" type="time" value={r.end_time} error={err(i, "end_time")} disabled={readOnly} onChange={(v) => update(i, { end_time: v })} />
                    <RuleInput label="Slot length" type="number" value={r.slot_minutes} error={err(i, "slot_minutes")} disabled={readOnly} onChange={(v) => update(i, { slot_minutes: Number(v) })} />
                    <td className="px-2 py-1.5">
                      <div className="flex gap-1">
                        <NumberBox label="Buffer before" value={r.buffer_before_minutes} disabled={readOnly} onChange={(v) => update(i, { buffer_before_minutes: v })} />
                        <NumberBox label="Buffer after" value={r.buffer_after_minutes} disabled={readOnly} onChange={(v) => update(i, { buffer_after_minutes: v })} />
                      </div>
                      {err(i, "buffer_before_minutes") || err(i, "buffer_after_minutes") ? <p className="text-xs text-danger">{err(i, "buffer_before_minutes") ?? err(i, "buffer_after_minutes")}</p> : null}
                    </td>
                    <RuleInput label="Minimum notice" type="number" value={r.min_notice_minutes} error={err(i, "min_notice_minutes")} disabled={readOnly} onChange={(v) => update(i, { min_notice_minutes: Number(v) })} />
                    <td className="px-2 py-1.5 text-right">
                      {!readOnly ? (
                        <Button size="sm" variant="ghost" aria-label={`Remove ${WEEKDAYS[r.weekday]} ${r.start_time}–${r.end_time}`} onClick={() => setRules((rs) => (rs ? rs.filter((_, k) => k !== i) : rs))}>
                          <Trash2 className="h-4 w-4" aria-hidden />
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!readOnly ? (
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" onClick={() => setRules((rs) => [...(rs ?? []), { ...NEW_RULE, weekday: 0 }])}>
                <Plus className="h-4 w-4" aria-hidden /> Add window
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setRules((rs) => [...(rs ?? []), ...[0, 1, 2, 3, 4].map((weekday) => ({ ...NEW_RULE, weekday }))])}
              >
                Add Mon–Fri 09:00–17:00
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </Dialog>
  );
}

function RuleInput({ label, type, value, error, disabled, onChange }: { label: string; type: "time" | "number"; value: string | number; error?: string; disabled: boolean; onChange: (v: string) => void }) {
  return (
    <td className="px-2 py-1.5">
      <Input aria-label={label} aria-invalid={error ? true : undefined} type={type} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={type === "number" ? "w-24" : "w-28"} />
      {error ? <p className="text-xs text-danger">{error}</p> : null}
    </td>
  );
}

function NumberBox({ label, value, disabled, onChange }: { label: string; value: number; disabled: boolean; onChange: (v: number) => void }) {
  return <Input aria-label={label} type="number" min={0} max={240} value={value} disabled={disabled} onChange={(e) => onChange(Number(e.target.value))} className="w-20" />;
}

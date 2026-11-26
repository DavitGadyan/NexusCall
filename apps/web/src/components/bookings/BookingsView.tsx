"use client";
/** Bookings: week / day calendar and a filtered list. Live `booking.updated` hints refresh the calendar. */
import { ChevronLeft, ChevronRight } from "lucide-react";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import { useCan } from "@/components/shell/session-context";
import { Badge, Button, Card, Checkbox, type Column, DataTable, EmptyState, ErrorState, Input, LoadingState, Select, Tabs } from "@/components/ui";
import { useLiveHint, useLiveRefresh } from "@/components/calls/use-live";
import { BookingDetailDialog, BookingFormDialog, statusTone, useResources } from "./BookingDialogs";
import { formatTime, fromDateInput, hourBounds, layoutDay, sameDay, shiftAnchor, toDateInput, type View, viewRange } from "./calendar";
import { BOOKING_STATUSES, type Booking, type Resource } from "./types";

const HOUR_PX = 48;

export function BookingsView() {
  const can = useCan();
  const [tab, setTab] = React.useState<"calendar" | "list">("calendar");
  const resources = useResources();
  const items = resources.data?.items ?? [];
  const [selected, setSelected] = React.useState<Booking | null>(null);
  const [creating, setCreating] = React.useState<{ start?: Date } | null>(null);
  const [nonce, setNonce] = React.useState(0);
  const changed = React.useCallback(() => setNonce((n) => n + 1), []);

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div />
        {can("bookings:manage") ? (
          <Button size="sm" onClick={() => setCreating({})} disabled={items.length === 0} data-testid="new-booking">
            New booking
          </Button>
        ) : null}
      </div>
      {resources.error ? <ErrorState problem={resources.error.problem} onRetry={resources.reload} title="Resources could not be loaded" /> : null}
      {!resources.loading && !resources.error && items.length === 0 ? (
        <Card>
          <EmptyState title="No bookable resources yet" description="Add a staff member or calendar under Resources & availability, then set its weekly hours." />
        </Card>
      ) : null}
      <Tabs
        label="Bookings view"
        value={tab}
        onChange={(id) => setTab(id as "calendar" | "list")}
        tabs={[
          { id: "calendar", label: "Calendar" },
          { id: "list", label: "List" },
        ]}
      >
        {tab === "calendar" ? (
          <CalendarView resources={items} onOpen={setSelected} onCreateAt={can("bookings:manage") ? (d) => setCreating({ start: d }) : undefined} nonce={nonce} />
        ) : (
          <BookingList resources={items} onOpen={setSelected} nonce={nonce} />
        )}
      </Tabs>
      <BookingDetailDialog booking={selected} resources={items} onClose={() => setSelected(null)} onChanged={changed} />
      <BookingFormDialog
        open={creating !== null}
        mode="create"
        resources={items}
        initialStart={creating?.start}
        onClose={() => setCreating(null)}
        onSaved={() => {
          setCreating(null);
          changed();
        }}
      />
    </>
  );
}

function ResourceFilter({ resources, value, onChange }: { resources: Resource[]; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label htmlFor="bk-res-filter" className="sr-only">
        Resource
      </label>
      <Select id="bk-res-filter" value={value} onChange={(e) => onChange(e.target.value)} className="w-48">
        <option value="">All resources</option>
        {resources.map((r) => (
          <option key={r.id} value={r.id}>
            {r.name}
            {r.status !== "active" ? " (inactive)" : ""}
          </option>
        ))}
      </Select>
    </div>
  );
}

function CalendarView({ resources, onOpen, onCreateAt, nonce }: { resources: Resource[]; onOpen: (b: Booking) => void; onCreateAt?: (d: Date) => void; nonce: number }) {
  const [view, setView] = React.useState<View>("week");
  const [anchor, setAnchor] = React.useState(() => new Date());
  const [resourceId, setResourceId] = React.useState("");
  const [includeCanceled, setIncludeCanceled] = React.useState(false);
  const { from, to, days } = viewRange(view, anchor);
  const cal = useApi<{ items: Booking[] }>(
    withQuery("/api/v1/bookings/calendar", { from: from.toISOString(), to: to.toISOString(), resource_id: resourceId, include_canceled: includeCanceled || undefined }),
  );
  React.useEffect(() => {
    if (nonce) cal.reload();
  }, [nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  useLiveRefresh(cal.reload, { events: ["booking.updated"], pollMs: 30_000 });
  const bookings = cal.data?.items ?? [];
  const perDay = days.map((d) => layoutDay(bookings, d));
  const bounds = hourBounds(perDay.flat());
  const hours = Array.from({ length: bounds.end - bounds.start }, (_, i) => bounds.start + i);
  const today = new Date();
  const resourceName = (id: string) => resources.find((r) => r.id === id)?.name ?? "Resource";
  const title =
    view === "week"
      ? `${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(days[0]!)} – ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(days[6]!)}`
      : new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(days[0]!);

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <div className="inline-flex rounded-md border border-border" role="group" aria-label="Calendar range">
          {(["week", "day"] as const).map((v) => (
            <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)} className={cn("px-3 py-1.5 text-sm capitalize", view === v ? "bg-surface-2 font-medium" : "text-muted hover:text-fg")}>
              {v}
            </button>
          ))}
        </div>
        <Button size="sm" variant="ghost" onClick={() => setAnchor((a) => shiftAnchor(view, a, -1))} aria-label={`Previous ${view}`}>
          <ChevronLeft className="h-4 w-4" aria-hidden />
        </Button>
        <Button size="sm" variant="secondary" onClick={() => setAnchor(new Date())}>
          Today
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setAnchor((a) => shiftAnchor(view, a, 1))} aria-label={`Next ${view}`}>
          <ChevronRight className="h-4 w-4" aria-hidden />
        </Button>
        <h2 className="mr-auto text-sm font-semibold" aria-live="polite">
          {title}
        </h2>
        <label htmlFor="bk-jump" className="sr-only">
          Go to date
        </label>
        <Input id="bk-jump" type="date" className="w-40" value={toDateInput(anchor)} onChange={(e) => { const d = fromDateInput(e.target.value); if (d) setAnchor(d); }} />
        <ResourceFilter resources={resources} value={resourceId} onChange={setResourceId} />
        <Checkbox checked={includeCanceled} onChange={(e) => setIncludeCanceled(e.target.checked)} label="Show canceled" />
      </div>
      {cal.loading && !cal.data ? (
        <LoadingState label="Loading calendar" />
      ) : cal.error && !cal.data ? (
        <ErrorState problem={cal.error.problem} onRetry={cal.reload} />
      ) : (
        <div className="overflow-x-auto">
          <div className={cn("grid", view === "week" ? "min-w-[760px] grid-cols-[56px_repeat(7,minmax(0,1fr))]" : "grid-cols-[56px_minmax(0,1fr)]")}>
            <div className="border-b border-border" />
            {days.map((d) => (
              <div key={d.toISOString()} className={cn("border-b border-l border-border px-2 py-1.5 text-xs", sameDay(d, today) && "bg-brand/5 font-semibold text-brand")}>
                {new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric" }).format(d)}
              </div>
            ))}
            <div className="relative" style={{ height: hours.length * HOUR_PX }}>
              {hours.map((h, i) => (
                <span key={h} className="absolute right-2 text-[11px] text-muted tabular" style={{ top: i * HOUR_PX - 6 }}>
                  {i === 0 ? "" : formatTime(new Date(2000, 0, 1, h))}
                </span>
              ))}
            </div>
            {days.map((d, di) => (
              <div key={d.toISOString()} className="relative border-l border-border" style={{ height: hours.length * HOUR_PX }} aria-label={`Bookings on ${d.toDateString()}`} role="list">
                {hours.map((h, i) =>
                  onCreateAt ? (
                    <button
                      key={h}
                      type="button"
                      tabIndex={-1}
                      aria-hidden
                      className="absolute inset-x-0 border-t border-border/70 hover:bg-surface-2/60"
                      style={{ top: i * HOUR_PX, height: HOUR_PX }}
                      onClick={() => onCreateAt(new Date(d.getFullYear(), d.getMonth(), d.getDate(), h))}
                    />
                  ) : (
                    <div key={h} className="absolute inset-x-0 border-t border-border/70" style={{ top: i * HOUR_PX, height: HOUR_PX }} />
                  ),
                )}
                {perDay[di]!.map((p) => {
                  const top = ((p.startMin - bounds.start * 60) / 60) * HOUR_PX;
                  const height = Math.max(18, ((p.endMin - p.startMin) / 60) * HOUR_PX - 2);
                  const width = 100 / p.lanes;
                  const canceled = p.booking.status === "canceled";
                  return (
                    <button
                      key={p.booking.id}
                      type="button"
                      role="listitem"
                      onClick={() => onOpen(p.booking)}
                      data-testid="calendar-booking"
                      className={cn(
                        "absolute overflow-hidden rounded-md border px-1.5 py-0.5 text-left text-[11px] leading-tight shadow-sm",
                        canceled ? "border-dashed border-border bg-surface text-muted line-through" : p.booking.status === "tentative" ? "border-warn/50 bg-warn/10" : "border-brand/40 bg-brand/10",
                      )}
                      style={{ top, height, left: `calc(${p.lane * width}% + 2px)`, width: `calc(${width}% - 4px)` }}
                    >
                      <span className="block font-medium tabular">
                        {formatTime(p.booking.start)} – {formatTime(p.booking.end)}
                      </span>
                      <span className="block truncate">{resourceName(p.booking.resource_id)}</span>
                      <span className="block truncate text-muted">{p.booking.status.replace(/_/g, " ")}</span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          {bookings.length === 0 ? <p className="border-t border-border px-4 py-3 text-center text-sm text-muted">No bookings in this {view}.{onCreateAt ? " Click a time to add one." : ""}</p> : null}
        </div>
      )}
    </Card>
  );
}

function BookingList({ resources, onOpen, nonce }: { resources: Resource[]; onOpen: (b: Booking) => void; nonce: number }) {
  const [status, setStatus] = React.useState("");
  const [resourceId, setResourceId] = React.useState("");
  const [fromDay, setFromDay] = React.useState("");
  const [toDay, setToDay] = React.useState("");
  const fromD = fromDateInput(fromDay);
  const toD = fromDateInput(toDay);
  const list = useCursorList<Booking>(
    withQuery("/api/v1/bookings", {
      status,
      resource_id: resourceId,
      from: fromD?.toISOString(),
      to: toD ? new Date(toD.getFullYear(), toD.getMonth(), toD.getDate() + 1).toISOString() : undefined,
    }),
  );
  const hint = useLiveHint({ events: ["booking.updated"] });
  React.useEffect(() => {
    if (nonce) list.reload();
  }, [nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  const resourceName = (id: string) => resources.find((r) => r.id === id)?.name ?? "—";
  const columns: Column<Booking>[] = [
    {
      key: "when",
      header: "When",
      cell: (b) => (
        <button type="button" className="text-left text-brand hover:underline" onClick={() => onOpen(b)}>
          {formatDateTime(b.start)}
          <span className="block text-xs text-muted">until {formatTime(b.end)}</span>
        </button>
      ),
    },
    { key: "resource", header: "Resource", cell: (b) => resourceName(b.resource_id) },
    { key: "status", header: "Status", cell: (b) => <Badge tone={statusTone(b.status)}>{b.status.replace(/_/g, " ")}</Badge> },
    { key: "source", header: "Booked via", cell: (b) => b.source_type.replace(/_/g, " "), hideOnMobile: true },
    { key: "sync", header: "Calendar sync", cell: (b) => b.calendar_sync_state?.replace(/_/g, " ") ?? "—", hideOnMobile: true },
  ];
  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="bk-status-filter" className="mb-1 block text-xs text-muted">
            Status
          </label>
          <Select id="bk-status-filter" value={status} onChange={(e) => setStatus(e.target.value)} className="w-40">
            <option value="">All</option>
            {BOOKING_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <span className="mb-1 block text-xs text-muted">Resource</span>
          <ResourceFilter resources={resources} value={resourceId} onChange={setResourceId} />
        </div>
        <div>
          <label htmlFor="bk-from" className="mb-1 block text-xs text-muted">
            From
          </label>
          <Input id="bk-from" type="date" value={fromDay} onChange={(e) => setFromDay(e.target.value)} className="w-40" />
        </div>
        <div>
          <label htmlFor="bk-to" className="mb-1 block text-xs text-muted">
            To
          </label>
          <Input id="bk-to" type="date" value={toDay} onChange={(e) => setToDay(e.target.value)} className="w-40" />
        </div>
        {hint.stale ? (
          <Button size="sm" variant="secondary" onClick={() => { hint.clear(); list.reload(); }}>
            New changes — refresh
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Bookings"
        columns={columns}
        rows={list.items}
        rowKey={(b) => b.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No bookings match" description="Bookings made by AI agents, campaigns and your team appear here." />}
      />
    </Card>
  );
}

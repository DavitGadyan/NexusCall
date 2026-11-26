"use client";
/**
 * Platform infrastructure: service readiness, broker queues and workers, and a metadata-only dead-letter
 * view. Message bodies are never fetched or shown; dead letters are identified by task/event name, tenant,
 * correlation id and short identifiers. Replay and discard need `platform:queues:replay`, a recent step-up
 * and a reason, and are audited.
 */
import { RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import * as React from "react";
import { api, withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { shortId } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ConfirmDialog, EmptyState, ErrorState, Field, Input, LoadingState, Notice, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { KpiTile } from "@/components/analytics/charts";
import { formatInt } from "@/components/analytics/lib/format";
import { Mono, StepUpHint } from "./common";
import {
  type DeadLetter,
  type DlqActionOut,
  failingChecks,
  formatRate,
  groupQueues,
  type QueueInfo,
  type QueuesOut,
  queueNote,
  queueTone,
  type ServiceHealth,
  serviceTone,
  sortServices,
  summarizeServices,
  validateDlqAction,
} from "./lib/infra";

export function InfraServices() {
  const services = useApi<{ items: ServiceHealth[] }>("/api/v1/platform/infra/services");
  const items = sortServices(services.data?.items ?? []);
  const sum = summarizeServices(items);
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <KpiTile label="Ready" value={services.data ? sum.ready : "—"} />
        <KpiTile label="Not ready" value={services.data ? sum.notReady : "—"} />
        <KpiTile label="Unreachable" value={services.data ? sum.unreachable : "—"} />
      </div>
      <Card>
        <CardHeader
          title="Services"
          description="Readiness probes run when this page loads. Unhealthy services are listed first."
          actions={
            <Button variant="secondary" size="sm" onClick={services.reload} disabled={services.loading}>
              <RefreshCw className="h-4 w-4" aria-hidden /> {services.loading ? "Checking…" : "Check again"}
            </Button>
          }
        />
        {services.loading && !services.data ? (
          <LoadingState label="Probing services" />
        ) : services.error && !services.data ? (
          <ErrorState problem={services.error.problem} onRetry={services.reload} />
        ) : items.length === 0 ? (
          <EmptyState title="No services reported" />
        ) : (
          <div className={services.loading ? "overflow-x-auto opacity-60" : "overflow-x-auto"}>
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Service health</caption>
              <thead>
                <tr className="text-xs uppercase tracking-wide text-muted">
                  <th scope="col" className="border-b border-border px-4 py-2">Service</th>
                  <th scope="col" className="border-b border-border px-4 py-2">Status</th>
                  <th scope="col" className="border-b border-border px-4 py-2">Latency</th>
                  <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Checks</th>
                </tr>
              </thead>
              <tbody>
                {items.map((s) => {
                  const failing = failingChecks(s);
                  return (
                    <tr key={s.service}>
                      <td className="border-b border-border px-4 py-2 font-mono text-xs">{s.service}</td>
                      <td className="border-b border-border px-4 py-2">
                        <Badge tone={serviceTone(s.status)}>{s.status.replace(/_/g, " ")}</Badge>
                      </td>
                      <td className="tabular border-b border-border px-4 py-2">{s.latency_ms !== null && s.latency_ms !== undefined ? `${s.latency_ms} ms` : "—"}</td>
                      <td className="hidden border-b border-border px-4 py-2 text-xs md:table-cell">
                        {Object.keys(s.checks ?? {}).length === 0 ? (
                          "—"
                        ) : failing.length ? (
                          <span className="text-danger">failing: {failing.join(", ")}</span>
                        ) : (
                          <span className="text-muted">{Object.keys(s.checks ?? {}).join(", ")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

const KIND_TITLES: Record<QueueInfo["kind"], { title: string; description: string }> = {
  dead_letter: { title: "Dead-letter queues", description: "Messages that failed every retry. Inspect metadata, then replay or discard." },
  task: { title: "Task queues", description: "Celery work queues and their workers." },
  event: { title: "Event queues", description: "Domain event consumers." },
  other: { title: "Other queues", description: "" },
};

export function InfraQueues() {
  const queues = useApi<QueuesOut>("/api/v1/platform/infra/queues");
  const [open, setOpen] = React.useState<string | null>(null);
  const groups = groupQueues(queues.data?.items ?? []);

  return (
    <div className="flex flex-col gap-4">
      {queues.data && queues.data.dead_letter_total > 0 ? (
        <Notice tone="danger" title={`${formatInt(queues.data.dead_letter_total)} dead letters waiting`}>
          Find the cause before replaying: a replayed message runs the same code again.
        </Notice>
      ) : null}
      <div className="flex justify-end">
        <Button variant="secondary" size="sm" onClick={queues.reload} disabled={queues.loading}>
          <RefreshCw className="h-4 w-4" aria-hidden /> {queues.loading ? "Refreshing…" : "Refresh"}
        </Button>
      </div>
      {queues.loading && !queues.data ? (
        <Card>
          <LoadingState label="Loading queues" />
        </Card>
      ) : queues.error && !queues.data ? (
        <Card>
          <ErrorState problem={queues.error.problem} onRetry={queues.reload} />
        </Card>
      ) : (
        (["dead_letter", "task", "event", "other"] as const)
          .filter((k) => groups[k].length > 0)
          .map((k) => (
            <Card key={k}>
              <CardHeader title={KIND_TITLES[k].title} description={KIND_TITLES[k].description || undefined} />
              <QueueTable items={groups[k]} onInspect={k === "dead_letter" ? setOpen : undefined} openQueue={open} />
              {k === "dead_letter" && open ? <DeadLetters queue={open} onChanged={queues.reload} onClose={() => setOpen(null)} /> : null}
            </Card>
          ))
      )}
    </div>
  );
}

function QueueTable({ items, onInspect, openQueue }: { items: QueueInfo[]; onInspect?: (q: string) => void; openQueue: string | null }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Queues</caption>
        <thead>
          <tr className="text-xs uppercase tracking-wide text-muted">
            <th scope="col" className="border-b border-border px-4 py-2">Queue</th>
            <th scope="col" className="border-b border-border px-4 py-2 text-right">Messages</th>
            <th scope="col" className="hidden border-b border-border px-4 py-2 text-right md:table-cell">Ready / unacked</th>
            <th scope="col" className="border-b border-border px-4 py-2 text-right">Consumers</th>
            <th scope="col" className="hidden border-b border-border px-4 py-2 text-right lg:table-cell">In / out</th>
            <th scope="col" className="border-b border-border px-4 py-2"><span className="sr-only">State</span></th>
          </tr>
        </thead>
        <tbody>
          {items.map((q) => {
            const note = queueNote(q);
            return (
              <tr key={q.name} className={openQueue === q.name ? "bg-surface-2/60" : undefined}>
                <td className="border-b border-border px-4 py-2 font-mono text-xs">{q.name}</td>
                <td className="tabular border-b border-border px-4 py-2 text-right">{formatInt(q.messages ?? 0)}</td>
                <td className="tabular hidden border-b border-border px-4 py-2 text-right md:table-cell">
                  {formatInt(q.ready ?? 0)} / {formatInt(q.unacked ?? 0)}
                </td>
                <td className="tabular border-b border-border px-4 py-2 text-right">{q.consumers ?? 0}</td>
                <td className="tabular hidden border-b border-border px-4 py-2 text-right text-xs lg:table-cell">
                  {formatRate(q.publish_rate)} / {formatRate(q.deliver_rate)}
                </td>
                <td className="border-b border-border px-4 py-2 text-right">
                  <span className="flex items-center justify-end gap-2">
                    {note ? <Badge tone={queueTone(q)}>{note}</Badge> : <Badge tone="ok">ok</Badge>}
                    {onInspect && (q.messages ?? 0) > 0 ? (
                      <Button size="sm" variant="secondary" onClick={() => onInspect(q.name)} aria-pressed={openQueue === q.name}>
                        Inspect
                      </Button>
                    ) : null}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DeadLetters({ queue, onChanged, onClose }: { queue: string; onChanged: () => void; onClose: () => void }) {
  const can = useCan();
  const toast = useToast();
  const letters = useApi<{ queue: string; items: DeadLetter[] }>(withQuery("/api/v1/platform/infra/dead-letters", { queue, limit: 20 }));
  const [action, setAction] = React.useState<"replay" | "discard" | null>(null);
  const [count, setCount] = React.useState("1");
  const [countError, setCountError] = React.useState<string | null>(null);
  const canReplay = can("platform:queues:replay");
  const items = letters.data?.items ?? [];

  function start(a: "replay" | "discard") {
    const errs = validateDlqAction({ queue, count, reason: "placeholder" });
    setCountError(errs.count ?? errs.queue ?? null);
    if (errs.count || errs.queue) return;
    setAction(a);
  }

  return (
    <div className="border-t border-border px-5 py-4" aria-label={`Dead letters in ${queue}`}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">
          Oldest dead letters in <span className="font-mono text-xs">{queue}</span>
        </p>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      <Notice tone="info">Metadata only: message bodies are never read into this console. Peeking leaves the messages in the queue.</Notice>
      <div className="mt-3">
        {letters.loading && !letters.data ? (
          <LoadingState label="Loading dead letters" />
        ) : letters.error && !letters.data ? (
          <ErrorState problem={letters.error.problem} onRetry={letters.reload} />
        ) : items.length === 0 ? (
          <EmptyState title="This queue is empty" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Dead letters</caption>
              <thead>
                <tr className="text-xs uppercase tracking-wide text-muted">
                  <th scope="col" className="border-b border-border px-3 py-2">Kind</th>
                  <th scope="col" className="border-b border-border px-3 py-2">Name</th>
                  <th scope="col" className="border-b border-border px-3 py-2">Reason</th>
                  <th scope="col" className="hidden border-b border-border px-3 py-2 md:table-cell">Tenant</th>
                  <th scope="col" className="hidden border-b border-border px-3 py-2 lg:table-cell">Identifiers</th>
                  <th scope="col" className="border-b border-border px-3 py-2 text-right">Deaths</th>
                </tr>
              </thead>
              <tbody>
                {items.map((d, i) => (
                  <tr key={`${d.correlation_id ?? "x"}-${i}`}>
                    <td className="border-b border-border px-3 py-2">
                      <Badge>{d.kind}</Badge>
                    </td>
                    <td className="border-b border-border px-3 py-2 font-mono text-xs">{d.name ?? "—"}</td>
                    <td className="border-b border-border px-3 py-2 text-xs">
                      {d.death_reason ?? "—"}
                      {d.first_death_queue ? <span className="block text-muted">from {d.first_death_queue}</span> : null}
                    </td>
                    <td className="hidden border-b border-border px-3 py-2 font-mono text-xs md:table-cell">{d.tenant_id ? shortId(d.tenant_id) : "—"}</td>
                    <td className="hidden border-b border-border px-3 py-2 lg:table-cell">
                      <Mono>
                        {Object.entries(d.identifiers ?? {})
                          .map(([k, v]) => `${k}=${v}`)
                          .join(" ") || (d.correlation_id ? `correlation=${shortId(d.correlation_id)}` : "—")}
                      </Mono>
                    </td>
                    <td className="tabular border-b border-border px-3 py-2 text-right">{d.death_count ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {canReplay && items.length > 0 ? (
        <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-border pt-4">
          <Field id="dlq-count" label="Messages" hint="Oldest first, 1–50 per action." error={countError} className="w-40">
            {(p) => <Input {...p} inputMode="numeric" value={count} onChange={(e) => setCount(e.target.value)} />}
          </Field>
          <Button onClick={() => start("replay")} data-testid="dlq-replay">
            <RotateCcw className="h-4 w-4" aria-hidden /> Replay…
          </Button>
          <Button variant="danger" onClick={() => start("discard")} data-testid="dlq-discard">
            <Trash2 className="h-4 w-4" aria-hidden /> Discard…
          </Button>
          <StepUpHint />
        </div>
      ) : null}
      <ConfirmDialog
        open={action !== null}
        onClose={() => setAction(null)}
        title={action === "replay" ? "Replay dead letters" : "Discard dead letters"}
        description={
          action === "replay"
            ? `The ${count} oldest message(s) go back to their original queue and run again.`
            : `The ${count} oldest message(s) are deleted permanently. This cannot be undone.`
        }
        confirmLabel={action === "replay" ? "Replay" : "Discard"}
        tone={action === "replay" ? "primary" : "danger"}
        typedConfirmation={action === "discard" ? "discard" : undefined}
        reason={{ minLength: 5, maxLength: 300 }}
        onConfirm={async (reason) => {
          if (!action) return;
          const out = await api<DlqActionOut>(`/api/v1/platform/infra/dead-letters/${action}`, { method: "POST", body: { queue, count: Number(count), reason } });
          toast({
            tone: "success",
            title: `${out.processed} message${out.processed === 1 ? "" : "s"} ${out.action}`,
            description: out.target ? `Sent to ${out.target}` : undefined,
          });
          letters.reload();
          onChanged();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </div>
  );
}

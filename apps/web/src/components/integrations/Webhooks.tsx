"use client";
/** Outbound webhook endpoints. Creating one needs `integrations:manage` and a recent step-up. */
import { Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, Dialog, EmptyState, FormError, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { StepUpHint } from "@/components/platform-ops/common";
import { EndpointFields } from "./EndpointForm";
import { type EndpointForm, eventLabel, validateEndpointForm } from "./lib/webhooks";
import type { Endpoint } from "./lib/types";

export function Webhooks() {
  const can = useCan();
  const list = useCursorList<Endpoint>("/api/v1/webhooks/endpoints");
  const [creating, setCreating] = React.useState(false);
  const canManage = can("integrations:manage");

  const columns: Column<Endpoint>[] = [
    {
      key: "url",
      header: "Endpoint",
      cell: (e) => (
        <Link href={`/app/integrations/webhooks/${e.id}`} className="block max-w-md text-brand hover:underline">
          <span className="block truncate font-mono text-xs">{e.url}</span>
          {e.description ? <span className="block truncate text-xs text-muted">{e.description}</span> : null}
        </Link>
      ),
    },
    { key: "events", header: "Events", hideOnMobile: true, cell: (e) => <span className="text-xs">{e.event_types.map(eventLabel).join(", ")}</span> },
    {
      key: "status",
      header: "Status",
      cell: (e) => (
        <span className="flex flex-col gap-0.5">
          <Badge tone={e.status === "active" ? (e.consecutive_failures > 0 ? "warn" : "ok") : "neutral"}>{e.status}</Badge>
          {e.consecutive_failures > 0 ? <span className="text-xs text-warn">{e.consecutive_failures} failing in a row</span> : null}
        </span>
      ),
    },
    { key: "last", header: "Last delivery", hideOnMobile: true, cell: (e) => <span title={formatDateTime(e.last_delivery_at)}>{relativeTime(e.last_delivery_at)}</span> },
  ];

  return (
    <Card>
      <CardHeader
        title="Webhooks"
        description="Signed HTTPS callbacks for workspace events. Failed deliveries are retried with backoff."
        actions={
          canManage ? (
            <Button onClick={() => setCreating(true)} data-testid="new-webhook">
              <Plus className="h-4 w-4" aria-hidden /> Add endpoint
            </Button>
          ) : null
        }
      />
      <DataTable
        caption="Webhook endpoints"
        columns={columns}
        rows={list.items}
        rowKey={(e) => e.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No webhook endpoints" description="Add an endpoint to receive lead, call, booking and email events." />}
      />
      <CreateEndpointDialog open={creating} onClose={() => setCreating(false)} />
    </Card>
  );
}

const EMPTY: EndpointForm = { url: "", description: "", event_types: [], secret: "" };

function CreateEndpointDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const [form, setForm] = React.useState<EndpointForm>(EMPTY);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setForm(EMPTY);
    setErrors({});
    setError(null);
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local = validateEndpointForm(form, { requireSecret: true });
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<Endpoint>("/api/v1/webhooks/endpoints", {
        method: "POST",
        body: { url: form.url.trim(), description: form.description.trim() || null, event_types: form.event_types, secret: form.secret },
      });
      setForm(EMPTY);
      toast({ tone: "success", title: "Webhook endpoint added" });
      onClose();
      router.push(`/app/integrations/webhooks/${out.id}`);
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Add webhook endpoint" size="lg">
      <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="off" noValidate>
        <StepUpHint />
        <EndpointFields value={form} onChange={setForm} errors={errors} disabled={busy} withSecret />
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} data-testid="save-webhook">
            {busy ? "Saving…" : "Add endpoint"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

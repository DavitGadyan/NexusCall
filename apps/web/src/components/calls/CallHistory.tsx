"use client";
/** Call history with filters (state, direction, campaign, contact) and cursor pagination. */
import { PhoneIncoming, PhoneOutgoing } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, EmptyState, Select } from "@/components/ui";
import { useAgentNames } from "./pickers";
import { CALL_STATES, callStateTone, formatDuration, isUuid } from "./shared";
import type { Call } from "./types";

export function CallHistory({ initial }: { initial: { state?: string; direction?: string; campaign_id?: string; contact_id?: string } }) {
  const [state, setState] = React.useState(initial.state && (CALL_STATES as readonly string[]).includes(initial.state) ? initial.state : "");
  const [direction, setDirection] = React.useState(initial.direction === "inbound" || initial.direction === "outbound" ? initial.direction : "");
  const [campaignId, setCampaignId] = React.useState(isUuid(initial.campaign_id) ? initial.campaign_id : "");
  const [contactId, setContactId] = React.useState(isUuid(initial.contact_id) ? initial.contact_id : "");
  const list = useCursorList<Call>(withQuery("/api/v1/calls", { state, direction, campaign_id: campaignId, contact_id: contactId }));
  const agentName = useAgentNames();
  const filtered = Boolean(state || direction || campaignId || contactId);

  const columns: Column<Call>[] = [
    {
      key: "when",
      header: "When",
      cell: (c) => (
        <Link href={`/app/calls/${c.id}`} className="block text-brand hover:underline" title={formatDateTime(c.created_at)}>
          {formatDateTime(c.created_at)}
          <span className="block text-xs text-muted">{relativeTime(c.created_at)}</span>
        </Link>
      ),
    },
    {
      key: "party",
      header: "Number",
      cell: (c) => (
        <span className="inline-flex items-center gap-1.5 font-mono text-xs">
          {c.direction === "inbound" ? <PhoneIncoming className="h-3.5 w-3.5" aria-label="Inbound" /> : <PhoneOutgoing className="h-3.5 w-3.5" aria-label="Outbound" />}
          {(c.direction === "inbound" ? c.from_masked : c.to_masked) ?? "—"}
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (c) => (
        <div className="flex flex-wrap gap-1">
          <Badge tone={callStateTone(c.state)}>{c.state.replace(/_/g, " ")}</Badge>
          {c.is_test ? <Badge tone="brand">test</Badge> : null}
        </div>
      ),
    },
    {
      key: "outcome",
      header: "Outcome",
      cell: (c) =>
        c.outcome ? (
          <span>
            {c.outcome.replace(/_/g, " ")}
            {c.qualified ? <Badge tone="ok">qualified</Badge> : null}
          </span>
        ) : (
          <span className="text-muted">{c.end_reason?.replace(/_/g, " ") ?? c.blocked_reason?.replace(/_/g, " ") ?? "—"}</span>
        ),
    },
    { key: "duration", header: "Duration", cell: (c) => <span className="tabular">{formatDuration(c.duration_seconds)}</span>, hideOnMobile: true },
    { key: "agent", header: "Agent", cell: (c) => <span className="text-muted">{agentName(c.agent_id)}</span>, hideOnMobile: true },
  ];

  return (
    <Card>
      <CardHeader title="Calls" description="Newest first. Open a call for its timeline, outcome, transcript and recording." />
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3">
        <div>
          <label htmlFor="calls-state" className="mb-1 block text-xs text-muted">
            State
          </label>
          <Select id="calls-state" value={state} onChange={(e) => setState(e.target.value)} className="w-40">
            <option value="">All states</option>
            {CALL_STATES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label htmlFor="calls-direction" className="mb-1 block text-xs text-muted">
            Direction
          </label>
          <Select id="calls-direction" value={direction} onChange={(e) => setDirection(e.target.value)} className="w-36">
            <option value="">Both</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </Select>
        </div>
        {campaignId ? (
          <Badge tone="brand">
            Campaign {campaignId.slice(0, 8)}…
            <button type="button" className="ml-1 underline" onClick={() => setCampaignId("")} aria-label="Remove campaign filter">
              ×
            </button>
          </Badge>
        ) : null}
        {contactId ? (
          <Badge tone="brand">
            Contact {contactId.slice(0, 8)}…
            <button type="button" className="ml-1 underline" onClick={() => setContactId("")} aria-label="Remove contact filter">
              ×
            </button>
          </Badge>
        ) : null}
        {filtered ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setState("");
              setDirection("");
              setCampaignId("");
              setContactId("");
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Call history"
        columns={columns}
        rows={list.items}
        rowKey={(c) => c.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title={filtered ? "No calls match these filters" : "No calls yet"} description={filtered ? "Try a different state or direction." : "Calls placed by campaigns, inbound calls and test calls appear here."} />}
      />
    </Card>
  );
}

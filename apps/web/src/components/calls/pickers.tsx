"use client";
/** Lookups used by several forms: a debounced contact search and an AI agent select. */
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import type { Page } from "@/lib/types";
import { Input, Select } from "@/components/ui";
import { type FieldControlProps } from "@/components/ui/field";
import { useCan } from "@/components/shell/session-context";
import { contactLabel } from "./shared";
import type { Agent, Contact } from "./types";

export function useAgents(kind?: "voice" | "email") {
  const can = useCan();
  return useApi<Page<Agent>>(can("ai:agents:read") ? withQuery("/api/v1/ai/agents", { kind, limit: 100 }) : null);
}

export function AgentSelect({
  kind,
  value,
  onChange,
  control,
  allowNone,
}: {
  kind?: "voice" | "email";
  value: string;
  onChange: (id: string) => void;
  control: FieldControlProps;
  allowNone?: boolean;
}) {
  const agents = useAgents(kind);
  const can = useCan();
  if (!can("ai:agents:read")) {
    return <p className="text-xs text-muted">You need permission to view AI agents to choose one.</p>;
  }
  if (agents.error) {
    return <p className="text-xs text-danger">Agents could not be loaded ({agents.error.message})</p>;
  }
  return (
    <Select {...control} value={value} onChange={(e) => onChange(e.target.value)} disabled={agents.loading}>
      <option value="">{agents.loading ? "Loading agents…" : allowNone ? "No agent" : "Select an agent"}</option>
      {(agents.data?.items ?? []).map((a) => (
        <option key={a.id} value={a.id} disabled={a.status !== "active" && a.id !== value}>
          {a.name}
          {a.status !== "active" ? ` (${a.status})` : ""}
        </option>
      ))}
    </Select>
  );
}

/** Search contacts by name or email and pick one. Shows only what the user may already see in the CRM. */
export function ContactPicker({
  value,
  onChange,
  control,
}: {
  value: Contact | null;
  onChange: (c: Contact | null) => void;
  control: FieldControlProps;
}) {
  const can = useCan();
  const allowed = can("leads:read");
  const [q, setQ] = React.useState("");
  const [query, setQuery] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);
  const results = useApi<Page<Contact>>(allowed && query.length >= 2 ? withQuery("/api/v1/contacts", { q: query, limit: 8 }) : null);
  const listId = `${control.id}-results`;

  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm">
        <span className="min-w-0 truncate">
          <span className="font-medium">{contactLabel(value)}</span>
          {value.company ? <span className="text-muted"> · {value.company}</span> : null}
        </span>
        <button type="button" className="text-sm text-brand hover:underline" onClick={() => onChange(null)}>
          Change
        </button>
      </div>
    );
  }
  if (!allowed) return <p className="text-xs text-muted">You need permission to view contacts to choose one.</p>;
  const items = results.data?.items ?? [];
  return (
    <div className="flex flex-col gap-1">
      <Input
        {...control}
        type="search"
        autoComplete="off"
        placeholder="Search by name or email (2+ characters)"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        aria-controls={listId}
      />
      <div id={listId} aria-live="polite">
        {results.loading ? <p className="px-1 text-xs text-muted">Searching…</p> : null}
        {results.error ? <p className="px-1 text-xs text-danger">{results.error.message}</p> : null}
        {query.length >= 2 && !results.loading && !results.error && items.length === 0 ? <p className="px-1 text-xs text-muted">No matching contacts.</p> : null}
        {items.length > 0 ? (
          <ul className="max-h-48 overflow-auto rounded-md border border-border">
            {items.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onChange(c)}
                  className="flex w-full items-center justify-between gap-2 border-b border-border px-3 py-1.5 text-left text-sm last:border-b-0 hover:bg-surface-2 focus:bg-surface-2"
                >
                  <span className="truncate">{contactLabel(c)}</span>
                  <span className="shrink-0 text-xs text-muted">{c.status.replace(/_/g, " ")}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}

/** Resolves agent names for display; falls back to a short id. */
export function useAgentNames(): (id: string | null | undefined) => string {
  const agents = useAgents();
  return React.useCallback(
    (id) => {
      if (!id) return "—";
      return agents.data?.items.find((a) => a.id === id)?.name ?? `${id.slice(0, 8)}…`;
    },
    [agents.data],
  );
}

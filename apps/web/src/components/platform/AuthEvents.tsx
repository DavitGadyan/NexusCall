"use client";
/** Authentication events from the IdP and IdP status / OIDC metadata (build.md §20.2). */
import Link from "next/link";
import * as React from "react";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, shortId } from "@/lib/format";
import type { AuthEvent, IdpStatus, Page } from "@/lib/types";
import { Badge, Card, CardHeader, type Column, DataTable, EmptyState, ErrorState, KeyValues, LoadingState, Select, Tabs } from "@/components/ui";

const FAILURE = /ERROR$/;

export function AuthEventsAndIdp() {
  const [tab, setTab] = React.useState("events");
  return (
    <Tabs
      label="Identity provider"
      value={tab}
      onChange={setTab}
      tabs={[
        { id: "events", label: "Authentication events" },
        { id: "idp", label: "IdP status" },
      ]}
    >
      {tab === "events" ? <AuthEventsTable /> : <IdpStatusCard />}
    </Tabs>
  );
}

function AuthEventsTable() {
  const [limit, setLimit] = React.useState(100);
  const [onlyFailures, setOnlyFailures] = React.useState(false);
  const events = useApi<Page<AuthEvent>>(`/api/v1/platform/iam/auth-events?limit=${limit}`);
  const rows = (events.data?.items ?? []).filter((e) => !onlyFailures || FAILURE.test(e.type ?? "") || e.error);
  const columns: Column<AuthEvent & { _k: string }>[] = [
    { key: "time", header: "Time", cell: (e) => <span className="tabular whitespace-nowrap">{formatDateTime(e.time)}</span> },
    { key: "type", header: "Event", cell: (e) => <Badge tone={FAILURE.test(e.type ?? "") ? "danger" : "neutral"}>{e.type ?? "—"}</Badge> },
    {
      key: "user",
      header: "User",
      cell: (e) => (e.user_id ? <Link className="font-mono text-xs text-brand hover:underline" href={`/platform/users/${e.user_id}`}>{shortId(e.user_id)}</Link> : "—"),
    },
    { key: "client", header: "Client", cell: (e) => <span className="font-mono text-xs">{e.client_id ?? "—"}</span>, hideOnMobile: true },
    { key: "ip", header: "IP", cell: (e) => <span className="font-mono text-xs">{e.ip ?? "—"}</span>, hideOnMobile: true },
    { key: "error", header: "Error", cell: (e) => (e.error ? <span className="text-danger">{e.error}</span> : "—") },
  ];
  return (
    <Card>
      <CardHeader title="Authentication events" description="Logins, failures, logouts, token refresh errors and MFA changes. Codes and tokens are stripped." />
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3 text-sm">
        <label className="flex items-center gap-2">
          Show
          <Select value={String(limit)} onChange={(e) => setLimit(Number(e.target.value))} className="w-24">
            {[50, 100, 250, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={onlyFailures} onChange={(e) => setOnlyFailures(e.target.checked)} className="h-4 w-4" />
          Failures only
        </label>
      </div>
      <DataTable
        caption="Authentication events"
        columns={columns}
        rows={rows.map((e, i) => ({ ...e, _k: `${e.time}-${i}` }))}
        rowKey={(e) => e._k}
        loading={events.loading}
        error={events.error}
        onRetry={events.reload}
        empty={<EmptyState title="No events" description="The identity provider returned no events for this range." />}
      />
    </Card>
  );
}

function IdpStatusCard() {
  const idp = useApi<IdpStatus>("/api/v1/platform/iam/idp");
  if (idp.loading && !idp.data) return <Card><LoadingState /></Card>;
  if (idp.error && !idp.data) return <Card><ErrorState problem={idp.error.problem} onRetry={idp.reload} /></Card>;
  const s = idp.data;
  if (!s) return null;
  return (
    <Card>
      <CardHeader title="Identity provider" actions={<Badge tone={s.reachable === false ? "danger" : "ok"}>{s.reachable === false ? `Unreachable (${s.error ?? "error"})` : "Reachable"}</Badge>} />
      <div className="px-5 py-4">
        <KeyValues
          items={[
            { label: "Issuer", value: <span className="font-mono text-xs">{s.issuer ?? "—"}</span> },
            { label: "Audience", value: <span className="font-mono text-xs">{s.audience ?? "—"}</span> },
            { label: "Allowed algorithms", value: (s.allowed_algorithms ?? []).join(", ") || "—" },
            { label: "Realm", value: s.realm ?? "—" },
            { label: "Brute-force protection", value: s.brute_force_protected === undefined ? "—" : s.brute_force_protected ? "On" : <span className="text-danger">Off</span> },
            { label: "OTP policy", value: s.otp_policy ?? "—" },
            { label: "Access token lifetime", value: s.access_token_lifespan ? `${s.access_token_lifespan} s` : "—" },
            { label: "Refresh token rotation", value: s.revoke_refresh_token === undefined ? "—" : s.revoke_refresh_token ? "On (reuse detection)" : <span className="text-danger">Off</span> },
            { label: "Password policy", value: <span className="font-mono text-xs">{s.password_policy ?? "—"}</span> },
            {
              label: "Signing keys",
              value: (s.signing_keys ?? []).length ? (
                <ul className="flex flex-col gap-0.5">
                  {(s.signing_keys ?? []).map((k) => (
                    <li key={k.kid} className="font-mono text-xs">
                      {k.alg} · {k.kid}
                    </li>
                  ))}
                </ul>
              ) : (
                "—"
              ),
            },
          ]}
        />
      </div>
    </Card>
  );
}

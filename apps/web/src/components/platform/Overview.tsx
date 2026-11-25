"use client";
import Link from "next/link";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, humanize } from "@/lib/format";
import type { Overview as OverviewData } from "@/lib/types";
import { Badge, Card, CardHeader, ErrorState, KeyValues, LoadingState, Notice, Stat } from "@/components/ui";

export function Overview() {
  const { data, error, loading, reload } = useApi<OverviewData>("/api/v1/platform/overview");
  if (loading && !data) return <Card><LoadingState label="Loading overview" /></Card>;
  if (error && !data) return <Card><ErrorState problem={error.problem} onRetry={reload} /></Card>;
  if (!data) return null;
  const tenantsTotal = Object.values(data.tenants).reduce((a, b) => a + b, 0);
  const switches = [
    ["Calls", data.kill_switches.calls_enabled],
    ["Emails", data.kill_switches.emails_enabled],
    ["AI", data.kill_switches.ai_enabled],
  ] as const;
  const anyOff = switches.some(([, on]) => !on);
  return (
    <div className="flex flex-col gap-6">
      {data.maintenance.mode !== "off" ? (
        <Notice tone="warn" title={`Maintenance mode: ${humanize(data.maintenance.mode)}`}>
          {data.maintenance.message || "No message set."}
        </Notice>
      ) : null}
      {anyOff ? (
        <Notice tone="danger" title="A platform kill switch is active" action={<Link className="text-sm font-medium text-brand hover:underline" href="/platform/settings?tab=kill_switches">Review</Link>}>
          {data.kill_switches.reason || "No reason recorded."}
        </Notice>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Tenants" value={tenantsTotal} hint={Object.entries(data.tenants).map(([k, v]) => `${v} ${k}`).join(" · ") || "none yet"} />
        <Stat label="Users" value={data.users} />
        <Stat label="Active memberships" value={data.active_memberships} />
        <Stat label="Pending privileged requests" value={data.pending_privileged_requests} hint="Dual approval queue" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Kill switches" description="Platform-wide switches; tenant switches apply on top." actions={<Link className="text-sm text-brand hover:underline" href="/platform/settings?tab=kill_switches">Manage</Link>} />
          <div className="flex flex-wrap gap-2 px-5 py-4">
            {switches.map(([label, on]) => (
              <Badge key={label} tone={on ? "ok" : "danger"}>
                {label}: {on ? "enabled" : "stopped"}
              </Badge>
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="Last RLS coverage scan" actions={<Link className="text-sm text-brand hover:underline" href="/platform/security/rls">Open</Link>} />
          <div className="px-5 py-4">
            {data.last_rls_scan ? (
              <KeyValues
                items={[
                  { label: "Scanned", value: formatDateTime(data.last_rls_scan.at) },
                  { label: "Services", value: `${data.last_rls_scan.summary.services_scanned} scanned, ${data.last_rls_scan.summary.services_unreachable} unreachable` },
                  { label: "Tables", value: data.last_rls_scan.summary.tables },
                  {
                    label: "Issues",
                    value: (
                      <Badge tone={data.last_rls_scan.summary.issues + data.last_rls_scan.summary.role_issues > 0 ? "danger" : "ok"}>
                        {data.last_rls_scan.summary.issues} table, {data.last_rls_scan.summary.role_issues} role
                      </Badge>
                    ),
                  },
                ]}
              />
            ) : (
              <p className="text-sm text-muted">No scan has run yet.</p>
            )}
          </div>
        </Card>
      </div>
      <Card>
        <CardHeader title="Operations" />
        <ul className="grid gap-1 px-5 py-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
          {[
            ["/platform/tenants/new", "Create a tenant"],
            ["/platform/privileged-requests", "Privileged requests"],
            ["/platform/security/break-glass", "Break-glass & support access"],
            ["/platform/users", "Users & sessions"],
            ["/platform/auth-events", "Authentication events & IdP"],
            ["/platform/audit", "Platform audit"],
          ].map(([href, label]) => (
            <li key={href}>
              <Link className="text-brand hover:underline" href={href as string}>
                {label}
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

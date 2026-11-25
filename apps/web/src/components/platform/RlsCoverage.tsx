"use client";
/**
 * RLS coverage (build.md §20.1, §22.7–8). Runs the read-only coverage scan and shows results by service
 * and table. There is deliberately no control here that edits, disables or bypasses a policy: policies
 * change only through reviewed migrations deployed by CI.
 */
import { ScanSearch } from "lucide-react";
import * as React from "react";
import { api, toApiError, type ApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { isIssue, ROLE_ISSUES, sortServices, statusInfo, summarizeScan } from "@/lib/rls";
import type { RlsScan, RlsServiceResult } from "@/lib/types";
import { Badge, Button, Card, CardHeader, EmptyState, ErrorState, FormError, LoadingState, Notice, Stat } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";

export function RlsCoverage() {
  const can = useCan();
  const latest = useApi<{ scan: RlsScan | null }>("/api/v1/platform/rls/scans/latest");
  const [scanning, setScanning] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [onlyIssues, setOnlyIssues] = React.useState(true);

  async function runScan() {
    setScanning(true);
    setError(null);
    try {
      await api("/api/v1/platform/rls/scans", { method: "POST" });
      latest.reload();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setScanning(false);
    }
  }

  const scan = latest.data?.scan ?? null;
  const computed = scan ? summarizeScan(scan.results) : null;

  return (
    <div className="flex flex-col gap-4">
      <Notice tone="info" title="Read-only view">
        This page reports policy coverage. RLS policies are version-controlled migrations applied by the deployment identity; nothing here can change,
        disable or bypass them.
      </Notice>
      <Card>
        <CardHeader
          title="Coverage scan"
          description={scan ? `Last scan ${formatDateTime(scan.created_at)}` : "No scan has run yet."}
          actions={
            can("platform:security:read") ? (
              <Button onClick={runScan} disabled={scanning} data-testid="run-rls-scan">
                <ScanSearch className="h-4 w-4" aria-hidden /> {scanning ? "Scanning…" : "Run scan"}
              </Button>
            ) : null
          }
        />
        <div className="px-5 py-4">
          <FormError message={error && !error.isStepUp ? error.message : null} />
          {latest.loading && !latest.data ? (
            <LoadingState />
          ) : latest.error && !latest.data ? (
            <ErrorState problem={latest.error.problem} onRetry={latest.reload} />
          ) : computed ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="rls-summary">
              <Stat label="Services scanned" value={computed.summary.services_scanned} />
              <Stat label="Unreachable" value={computed.summary.services_unreachable} />
              <Stat label="Tables" value={computed.summary.tables} />
              <Stat label="Table issues" value={computed.summary.issues} />
              <Stat label="Role issues" value={computed.summary.role_issues} />
            </div>
          ) : (
            <EmptyState title="No scan yet" description="Run a scan to inspect every service database." />
          )}
        </div>
      </Card>

      {computed && computed.roleIssues.length > 0 ? (
        <Notice tone="danger" title="Unsafe database role configuration">
          <ul className="list-disc pl-5">
            {computed.roleIssues.map((r) => (
              <li key={`${r.service}-${r.issue}`}>
                <span className="font-mono">{r.service}</span>: {ROLE_ISSUES[r.issue] ?? r.issue}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}

      {scan ? (
        <>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} className="h-4 w-4" />
            Show only tables with issues
          </label>
          {sortServices(scan.results).map((r) => (
            <ServiceResult key={r.service} result={r} onlyIssues={onlyIssues} />
          ))}
        </>
      ) : null}
    </div>
  );
}

function ServiceResult({ result, onlyIssues }: { result: RlsServiceResult; onlyIssues: boolean }) {
  const issues = (result.tables ?? []).filter((t) => isIssue(t.status));
  const rows = onlyIssues ? issues : (result.tables ?? []);
  const healthy = result.available && issues.length === 0 && (result.role_issues ?? []).length === 0;
  return (
    <Card>
      <CardHeader
        title={result.service}
        description={result.available ? `Database ${result.database ?? "—"} · runtime role ${result.app_role ?? "—"} · ${result.tables.length} tables` : `Unreachable (${result.error ?? "error"})`}
        actions={
          <Badge tone={!result.available ? "warn" : healthy ? "ok" : "danger"}>
            {!result.available ? "Not scanned" : healthy ? "Covered" : `${issues.length + (result.role_issues?.length ?? 0)} issue(s)`}
          </Badge>
        }
      />
      {result.available && rows.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">RLS coverage for {result.service}</caption>
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th scope="col" className="border-b border-border px-4 py-2">Table</th>
                <th scope="col" className="border-b border-border px-4 py-2">Status</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">tenant_id</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Enabled</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 md:table-cell">Forced</th>
                <th scope="col" className="border-b border-border px-4 py-2">Commands</th>
                <th scope="col" className="hidden border-b border-border px-4 py-2 lg:table-cell">Policies</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const info = statusInfo(t.status);
                return (
                  <tr key={`${t.schema}.${t.table}`}>
                    <td className="border-b border-border px-4 py-2 font-mono text-xs">
                      {t.schema}.{t.table}
                    </td>
                    <td className="border-b border-border px-4 py-2">
                      <span title={info.help}>
                        <Badge tone={info.tone}>{info.label}</Badge>
                      </span>
                    </td>
                    <td className="hidden border-b border-border px-4 py-2 md:table-cell">{t.has_tenant_id ? "yes" : "no"}</td>
                    <td className="hidden border-b border-border px-4 py-2 md:table-cell">{t.rls_enabled ? "yes" : "no"}</td>
                    <td className="hidden border-b border-border px-4 py-2 md:table-cell">{t.rls_forced ? "yes" : "no"}</td>
                    <td className="border-b border-border px-4 py-2 font-mono text-xs">{t.commands.join(", ") || "—"}</td>
                    <td className="hidden border-b border-border px-4 py-2 font-mono text-xs lg:table-cell">{t.policies.join(", ") || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : result.available ? (
        <p className="px-5 py-3 text-sm text-muted">{onlyIssues ? "No issues: every tenant table has RLS enabled, forced and complete." : "No tables."}</p>
      ) : null}
    </Card>
  );
}

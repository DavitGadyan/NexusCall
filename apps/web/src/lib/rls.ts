/** RLS coverage scan presentation (build.md §20.1). Read-only: nothing here changes a policy. */
import type { RlsServiceResult, RlsSummary } from "./types";

export const RLS_STATUS: Record<string, { label: string; tone: "ok" | "neutral" | "danger" | "warn"; help: string }> = {
  ok: { label: "OK", tone: "ok", help: "RLS enabled and forced, policies cover every command." },
  platform_scoped: { label: "Platform scoped", tone: "neutral", help: "Reviewed exemption: a platform-global table with a reason comment." },
  missing_tenant_id: { label: "Missing tenant_id", tone: "danger", help: "Table has no tenant_id column and is not marked platform-scoped." },
  rls_disabled: { label: "RLS disabled", tone: "danger", help: "Row-level security is not enabled on this table." },
  rls_not_forced: { label: "RLS not forced", tone: "danger", help: "The table owner would bypass the policies (FORCE ROW LEVEL SECURITY missing)." },
  incomplete_policies: { label: "Incomplete policies", tone: "danger", help: "Not every command (SELECT/INSERT/UPDATE/DELETE) has a policy." },
  append_only_violated: { label: "Append-only violated", tone: "danger", help: "An append-only table allows UPDATE or DELETE." },
};

export const ROLE_ISSUES: Record<string, string> = {
  app_role_not_found: "The runtime database role was not found.",
  app_role_is_superuser: "The runtime role is a superuser (bypasses RLS).",
  app_role_bypasses_rls: "The runtime role has BYPASSRLS.",
};

export function statusInfo(status: string) {
  return RLS_STATUS[status] ?? { label: status, tone: "warn" as const, help: "Unknown status reported by the service." };
}

export function isIssue(status: string): boolean {
  return status !== "ok" && status !== "platform_scoped";
}

export type ScanIssue = { service: string; table: string; status: string };
export type RoleIssue = { service: string; issue: string };

/** Recomputes the summary from stored results (the latest-scan endpoint returns results only). */
export function summarizeScan(results: readonly RlsServiceResult[]): {
  summary: RlsSummary;
  issues: ScanIssue[];
  roleIssues: RoleIssue[];
} {
  const issues: ScanIssue[] = [];
  const roleIssues: RoleIssue[] = [];
  let tables = 0;
  for (const r of results) {
    tables += r.tables?.length ?? 0;
    for (const t of r.tables ?? []) if (isIssue(t.status)) issues.push({ service: r.service, table: `${t.schema}.${t.table}`, status: t.status });
    for (const i of r.role_issues ?? []) roleIssues.push({ service: r.service, issue: i });
  }
  return {
    summary: {
      services_scanned: results.filter((r) => r.available).length,
      services_unreachable: results.filter((r) => !r.available).length,
      tables,
      issues: issues.length,
      role_issues: roleIssues.length,
    },
    issues,
    roleIssues,
  };
}

/** Services with problems first, then alphabetical. */
export function sortServices(results: readonly RlsServiceResult[]): RlsServiceResult[] {
  const score = (r: RlsServiceResult) =>
    !r.available ? 1 : (r.role_issues?.length ?? 0) + (r.tables ?? []).filter((t) => isIssue(t.status)).length > 0 ? 0 : 2;
  return [...results].sort((a, b) => score(a) - score(b) || a.service.localeCompare(b.service));
}

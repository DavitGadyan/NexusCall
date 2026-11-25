import { describe, expect, it } from "vitest";
import { isIssue, sortServices, statusInfo, summarizeScan } from "./rls";
import type { RlsServiceResult } from "./types";

const t = (table: string, status: string) => ({
  table,
  schema: "public",
  status,
  rls_enabled: status !== "rls_disabled",
  rls_forced: status === "ok",
  has_tenant_id: status !== "missing_tenant_id",
  policies: [],
  commands: [],
});

const results: RlsServiceResult[] = [
  { service: "tenant-service", available: true, role_issues: [], tables: [t("memberships", "ok"), t("roles", "platform_scoped")] },
  { service: "crm-service", available: true, role_issues: ["app_role_bypasses_rls"], tables: [t("contacts", "rls_not_forced"), t("tags", "missing_tenant_id")] },
  { service: "voice-service", available: false, error: "ConnectError", tables: [] },
];

describe("RLS scan summary", () => {
  it("counts issues, role issues and unreachable services", () => {
    const { summary, issues, roleIssues } = summarizeScan(results);
    expect(summary).toEqual({ services_scanned: 2, services_unreachable: 1, tables: 4, issues: 2, role_issues: 1 });
    expect(issues).toEqual([
      { service: "crm-service", table: "public.contacts", status: "rls_not_forced" },
      { service: "crm-service", table: "public.tags", status: "missing_tenant_id" },
    ]);
    expect(roleIssues).toEqual([{ service: "crm-service", issue: "app_role_bypasses_rls" }]);
  });

  it("classifies statuses", () => {
    expect(isIssue("ok")).toBe(false);
    expect(isIssue("platform_scoped")).toBe(false);
    expect(isIssue("rls_disabled")).toBe(true);
    expect(statusInfo("rls_disabled").tone).toBe("danger");
    expect(statusInfo("something_new").tone).toBe("warn");
  });

  it("puts services with problems first", () => {
    expect(sortServices(results).map((r) => r.service)).toEqual(["crm-service", "voice-service", "tenant-service"]);
  });
});

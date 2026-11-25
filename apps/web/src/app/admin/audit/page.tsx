import { PageHeader } from "@/components/ui";
import { AuditLogViewer } from "@/components/admin/AuditLogViewer";

export const metadata = { title: "Audit log" };

export default function TenantAuditPage() {
  return (
    <>
      <PageHeader title="Audit log" description="Security-relevant actions in this workspace: who did what, when, and the outcome. Records are append-only." />
      <AuditLogViewer endpoint="/api/v1/audit/events" />
    </>
  );
}

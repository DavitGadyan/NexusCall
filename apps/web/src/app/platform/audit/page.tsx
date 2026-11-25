import { PageHeader } from "@/components/ui";
import { AuditLogViewer } from "@/components/admin/AuditLogViewer";

export const metadata = { title: "Platform audit" };

export default function PlatformAuditPage() {
  return (
    <>
      <PageHeader title="Platform audit" description="Platform and tenant audit events, including privileged and break-glass actions. Filter by tenant to review one workspace." />
      <AuditLogViewer endpoint="/api/v1/platform/audit/events" platform />
    </>
  );
}

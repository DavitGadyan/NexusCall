import { PageHeader } from "@/components/ui";
import { RlsCoverage } from "@/components/platform/RlsCoverage";

export const metadata = { title: "RLS coverage" };

export default function RlsPage() {
  return (
    <>
      <PageHeader
        title="RLS coverage"
        description="Row-level security across every service database: enabled and forced policies, tenant_id coverage and runtime role safety."
      />
      <RlsCoverage />
    </>
  );
}

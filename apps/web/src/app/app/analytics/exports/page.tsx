import { PageHeader } from "@/components/ui";
import { AnalyticsNav } from "@/components/analytics/common";
import { Exports } from "@/components/analytics/Exports";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Analytics exports" };

export default function Page() {
  return (
    <PermissionGate permission="analytics:export" what="analytics exports" back={WORKSPACE_BACK}>
      <PageHeader title="Analytics" description="Audited exports of daily metrics." />
      <AnalyticsNav />
      <Exports />
    </PermissionGate>
  );
}

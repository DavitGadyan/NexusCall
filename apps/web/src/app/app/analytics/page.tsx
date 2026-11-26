import { PageHeader } from "@/components/ui";
import { AnalyticsNav } from "@/components/analytics/common";
import { Overview } from "@/components/analytics/Overview";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Analytics" };

export default function Page() {
  return (
    <PermissionGate permission="analytics:read" what="analytics" back={WORKSPACE_BACK}>
      <PageHeader title="Analytics" description="Calls, email, AI drafts, bookings and costs for this workspace. Every day is a UTC calendar day." />
      <AnalyticsNav />
      <Overview />
    </PermissionGate>
  );
}

import { PageHeader } from "@/components/ui";
import { CampaignPicker } from "@/components/analytics/Campaigns";
import { AnalyticsNav } from "@/components/analytics/common";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Campaign analytics" };

export default function Page() {
  return (
    <PermissionGate permission="analytics:read" what="analytics" back={WORKSPACE_BACK}>
      <PageHeader title="Analytics" description="Results per campaign." />
      <AnalyticsNav />
      <CampaignPicker />
    </PermissionGate>
  );
}

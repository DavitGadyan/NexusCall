import { notFound } from "next/navigation";
import { PageHeader } from "@/components/ui";
import { CampaignReport } from "@/components/analytics/Campaigns";
import { AnalyticsNav } from "@/components/analytics/common";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Campaign analytics" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <PermissionGate permission="analytics:read" what="analytics" back={WORKSPACE_BACK}>
      <PageHeader title="Campaign report" description="Totals since launch; the trend below follows the selected UTC range." />
      <AnalyticsNav />
      <CampaignReport campaignId={id} />
    </PermissionGate>
  );
}

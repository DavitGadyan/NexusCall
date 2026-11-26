import { ModuleGate } from "@/components/crm/common";
import { CampaignBuilder } from "@/components/campaigns/CampaignBuilder";

export const metadata = { title: "New call campaign" };

export default function Page() {
  return (
    <ModuleGate permission="campaigns:create" what="campaign creation">
      <CampaignBuilder campaignId={null} />
    </ModuleGate>
  );
}

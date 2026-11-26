import { notFound } from "next/navigation";
import { ModuleGate } from "@/components/crm/common";
import { CampaignBuilder } from "@/components/campaigns/CampaignBuilder";

export const metadata = { title: "Edit call campaign" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <ModuleGate permission="campaigns:create" what="campaign editing">
      <CampaignBuilder campaignId={id} />
    </ModuleGate>
  );
}

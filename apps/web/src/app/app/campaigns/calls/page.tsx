import { PageHeader } from "@/components/ui";
import { ModuleGate } from "@/components/crm/common";
import { Campaigns } from "@/components/campaigns/Campaigns";

export const metadata = { title: "Call campaigns" };

export default function Page() {
  return (
    <ModuleGate permission="campaigns:read" what="call campaigns">
      <PageHeader title="Call campaigns" description="Build, launch and monitor outbound AI calling." />
      <Campaigns />
    </ModuleGate>
  );
}

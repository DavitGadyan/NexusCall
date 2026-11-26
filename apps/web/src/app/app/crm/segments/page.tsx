import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Segments } from "@/components/crm/Segments";

export const metadata = { title: "Segments" };

export default function Page() {
  return (
    <ModuleGate permission="campaigns:read" what="segments">
      <PageHeader title="Segments" description="Audiences for campaigns, built from contact data." />
      <CrmNav />
      <Segments />
    </ModuleGate>
  );
}

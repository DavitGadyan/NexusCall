import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Leads } from "@/components/crm/Leads";

export const metadata = { title: "Leads" };

export default function Page() {
  return (
    <ModuleGate permission="leads:read" what="leads">
      <PageHeader title="Leads" description="Qualify, follow up and export leads." />
      <CrmNav />
      <Leads />
    </ModuleGate>
  );
}

import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Suppressions } from "@/components/crm/Suppressions";

export const metadata = { title: "Suppressions" };

export default function Page() {
  return (
    <ModuleGate permission="leads:read" what="suppressions">
      <PageHeader title="Suppressions" description="Addresses and contacts that must never be contacted." />
      <CrmNav />
      <Suppressions />
    </ModuleGate>
  );
}

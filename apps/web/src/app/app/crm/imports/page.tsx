import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Imports } from "@/components/crm/Imports";

export const metadata = { title: "Imports" };

export default function Page() {
  return (
    <ModuleGate permission="leads:create" what="imports">
      <PageHeader title="Imports" description="Bring contacts in from CSV files." />
      <CrmNav />
      <Imports />
    </ModuleGate>
  );
}

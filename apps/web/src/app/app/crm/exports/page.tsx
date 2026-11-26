import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Exports } from "@/components/crm/Exports";

export const metadata = { title: "Exports" };

export default function Page() {
  return (
    <ModuleGate permission="leads:export" what="exports">
      <PageHeader title="Exports" description="Audited exports of lead data." />
      <CrmNav />
      <Exports />
    </ModuleGate>
  );
}

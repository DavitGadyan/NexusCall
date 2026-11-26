import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { Contacts } from "@/components/crm/Contacts";

export const metadata = { title: "Contacts" };

export default function Page() {
  return (
    <ModuleGate permission="leads:read" what="contacts">
      <PageHeader title="Contacts" description="Everyone you can reach, with consent and suppression state." />
      <CrmNav />
      <Contacts />
    </ModuleGate>
  );
}

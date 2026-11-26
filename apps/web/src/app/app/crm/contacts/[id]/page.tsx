import { notFound } from "next/navigation";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { ContactDetail } from "@/components/crm/ContactDetail";

export const metadata = { title: "Contact" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <ModuleGate permission="leads:read" what="contacts">
      <CrmNav />
      <ContactDetail contactId={id} />
    </ModuleGate>
  );
}

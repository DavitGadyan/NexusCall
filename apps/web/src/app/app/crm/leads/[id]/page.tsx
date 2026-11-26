import { notFound } from "next/navigation";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { LeadDetail } from "@/components/crm/LeadDetail";

export const metadata = { title: "Lead" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <ModuleGate permission="leads:read" what="leads">
      <CrmNav />
      <LeadDetail leadId={id} />
    </ModuleGate>
  );
}

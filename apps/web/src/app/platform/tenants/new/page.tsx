import { PageHeader } from "@/components/ui";
import { CreateTenantWizard } from "@/components/platform/CreateTenantWizard";

export const metadata = { title: "Create tenant" };

export default function NewTenantPage() {
  return (
    <>
      <PageHeader title="Create tenant" description="A new client workspace with default settings and an owner invitation." />
      <CreateTenantWizard />
    </>
  );
}

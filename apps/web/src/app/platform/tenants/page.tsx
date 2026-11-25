import { PageHeader } from "@/components/ui";
import { TenantDirectory } from "@/components/platform/Tenants";

export const metadata = { title: "Tenants" };

export default function TenantsPage() {
  return (
    <>
      <PageHeader title="Tenants" description="Every client workspace on the platform, with lifecycle status." />
      <TenantDirectory />
    </>
  );
}

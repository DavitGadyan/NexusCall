import { PageHeader } from "@/components/ui";
import { RolesAndPermissions } from "@/components/platform/Roles";

export const metadata = { title: "Roles & permissions" };

export default function RolesPage() {
  return (
    <>
      <PageHeader title="Roles & permissions" description="Role-to-permission mappings and the controlled permission catalogue." />
      <RolesAndPermissions />
    </>
  );
}

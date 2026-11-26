import { PageHeader } from "@/components/ui";
import { ConnectionWizard } from "@/components/integrations/ConnectionWizard";
import { ADMIN_BACK, PermissionGate } from "@/components/platform-ops/common";

export const metadata = { title: "Add connection" };

export default function Page() {
  return (
    <PermissionGate permission="integrations:manage" what="adding connections" back={ADMIN_BACK}>
      <PageHeader title="Add a provider connection" description="Choose a provider, enter its credentials once, and the server tests them." />
      <ConnectionWizard basePath="/admin/connections" />
    </PermissionGate>
  );
}

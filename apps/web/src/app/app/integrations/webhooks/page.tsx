import { PageHeader } from "@/components/ui";
import { IntegrationsNav } from "@/components/integrations/common";
import { Webhooks } from "@/components/integrations/Webhooks";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Webhooks" };

export default function Page() {
  return (
    <PermissionGate permission="integrations:read" what="webhooks" back={WORKSPACE_BACK}>
      <PageHeader title="Integrations" description="Outbound webhooks are signed with a secret you choose; the platform never shows it back." />
      <IntegrationsNav />
      <Webhooks />
    </PermissionGate>
  );
}

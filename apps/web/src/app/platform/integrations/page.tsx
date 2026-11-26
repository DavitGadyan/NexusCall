import { PageHeader } from "@/components/ui";
import { PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { PlatformIntegrations } from "@/components/platform-ops/PlatformIntegrations";

export const metadata = { title: "Platform integrations" };

export default function Page() {
  return (
    <PermissionGate permission="platform:integrations:read" what="platform integrations" back={PLATFORM_BACK}>
      <PageHeader
        title="Platform integrations"
        description="The platform's own provider credentials. Write-only, tested server-side, owned by one service each; rotation and revocation need a recent multi-factor sign-in."
      />
      <PlatformIntegrations />
    </PermissionGate>
  );
}

import { PageHeader } from "@/components/ui";
import { InfraNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { InfraServices } from "@/components/platform-ops/Infra";

export const metadata = { title: "Services" };

export default function Page() {
  return (
    <PermissionGate permission="platform:infra:read" what="infrastructure" back={PLATFORM_BACK}>
      <PageHeader title="Infrastructure" description="Readiness of every platform service." />
      <InfraNav />
      <InfraServices />
    </PermissionGate>
  );
}

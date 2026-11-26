import { PageHeader } from "@/components/ui";
import { AiAllowlists } from "@/components/platform-ops/AiAllowlists";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Tenant model allowlists" };

export default function Page() {
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="Restrict the models individual tenants may use." />
      <AiPlatformNav />
      <AiAllowlists />
    </PermissionGate>
  );
}

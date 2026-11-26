import { PageHeader } from "@/components/ui";
import { AiPolicies } from "@/components/platform-ops/AiPolicies";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "AI policies" };

export default function Page() {
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="The platform tool policy: proposals need a second administrator's approval before they can be deployed." />
      <AiPlatformNav />
      <AiPolicies />
    </PermissionGate>
  );
}

import { PageHeader } from "@/components/ui";
import { AiSecurityEvents } from "@/components/platform-ops/AiSecurityEvents";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "AI security events" };

export default function Page() {
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="Security events raised by the gateway and tool execution across tenants." />
      <AiPlatformNav />
      <AiSecurityEvents />
    </PermissionGate>
  );
}

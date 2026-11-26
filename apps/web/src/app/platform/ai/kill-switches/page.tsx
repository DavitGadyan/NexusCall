import { PageHeader } from "@/components/ui";
import { AiKillSwitches } from "@/components/platform-ops/AiKillSwitches";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "AI kill switches" };

export default function Page() {
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="Emergency stops for AI traffic by platform, provider, model, tenant or agent." />
      <AiPlatformNav />
      <AiKillSwitches />
    </PermissionGate>
  );
}

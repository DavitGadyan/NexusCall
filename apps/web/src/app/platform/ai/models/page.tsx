import { PageHeader } from "@/components/ui";
import { AiModels } from "@/components/platform-ops/AiModels";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Models & routing" };

export default function Page() {
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="Providers, models, prices and routing. Changes need a recent multi-factor sign-in and are audited." />
      <AiPlatformNav />
      <AiModels />
    </PermissionGate>
  );
}

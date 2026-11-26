import { PageHeader } from "@/components/ui";
import { PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { PlatformUsage } from "@/components/platform-ops/PlatformUsage";

export const metadata = { title: "Usage" };

export default function Page() {
  return (
    <PermissionGate permission="platform:usage:read" what="platform usage" back={PLATFORM_BACK}>
      <PageHeader title="Usage" description="Calls, email and AI usage with costs, per tenant. Days are UTC." />
      <PlatformUsage />
    </PermissionGate>
  );
}

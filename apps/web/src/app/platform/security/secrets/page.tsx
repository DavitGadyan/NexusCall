import { PageHeader } from "@/components/ui";
import { PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { SecretsHealth } from "@/components/platform-ops/SecretsHealth";

export const metadata = { title: "Secrets health" };

export default function Page() {
  return (
    <PermissionGate permission="platform:secrets:read" what="secrets health" back={PLATFORM_BACK}>
      <PageHeader title="Secrets health" description="Where every credential lives, who owns it, and when it was last rotated, used and checked. Values are never shown." />
      <SecretsHealth />
    </PermissionGate>
  );
}

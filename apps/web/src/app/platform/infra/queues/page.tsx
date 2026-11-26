import { PageHeader } from "@/components/ui";
import { InfraNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { InfraQueues } from "@/components/platform-ops/Infra";

export const metadata = { title: "Queues & dead letters" };

export default function Page() {
  return (
    <PermissionGate permission="platform:infra:read" what="infrastructure" back={PLATFORM_BACK}>
      <PageHeader
        title="Infrastructure"
        description="Broker queues, workers and dead letters. Dead letters are shown as metadata only; replay and discard need a recent multi-factor sign-in and a reason, and are audited."
      />
      <InfraNav />
      <InfraQueues />
    </PermissionGate>
  );
}

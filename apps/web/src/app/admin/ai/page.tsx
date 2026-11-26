import { PageHeader } from "@/components/ui";
import { ADMIN_BACK, PermissionGate } from "@/components/platform-ops/common";
import { TenantAi } from "@/components/platform-ops/TenantAi";

export const metadata = { title: "AI settings" };

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <PermissionGate permission="tenant:manage" what="AI settings" back={ADMIN_BACK}>
      <PageHeader
        title="AI settings"
        description="Allowed models, budget and usage, data classification, approval rules and tools for this workspace. Controls fixed by the platform are shown as locked."
      />
      <TenantAi initialTab={typeof sp.tab === "string" ? sp.tab : undefined} />
    </PermissionGate>
  );
}

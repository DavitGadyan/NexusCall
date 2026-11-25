import { PageHeader } from "@/components/ui";
import { PlatformSettings } from "@/components/platform/PlatformSettings";

export const metadata = { title: "Platform settings" };

export default async function PlatformSettingsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <>
      <PageHeader
        title="Platform settings"
        description="Branding, security policy, quotas, retention, maintenance, modules, kill switches and feature flags. High-risk sections need step-up MFA; every change is versioned and audited."
      />
      <PlatformSettings initialTab={typeof sp.tab === "string" ? sp.tab : undefined} />
    </>
  );
}

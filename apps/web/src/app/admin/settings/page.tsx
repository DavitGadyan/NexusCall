import { PageHeader } from "@/components/ui";
import { SettingsEditor } from "@/components/settings/SettingsEditor";
import { TENANT_SETTINGS } from "@/lib/settings-descriptors";

export const metadata = { title: "Settings" };

export default async function TenantSettingsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  return (
    <>
      <PageHeader
        title="Workspace settings"
        description="Every change is validated against platform policy, versioned and recorded in the audit log. Controls fixed by the platform are shown as locked."
      />
      <SettingsEditor endpoint="/api/v1/settings" sections={TENANT_SETTINGS} initialTab={typeof params.tab === "string" ? params.tab : undefined} />
    </>
  );
}

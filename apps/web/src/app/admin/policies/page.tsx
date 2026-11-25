import { PageHeader } from "@/components/ui";
import { SettingsEditor } from "@/components/settings/SettingsEditor";
import { TENANT_SETTINGS } from "@/lib/settings-descriptors";

export const metadata = { title: "Communication policy" };

const POLICY_SECTIONS = TENANT_SETTINGS.filter((s) => ["communication", "retention", "approval"].includes(s.id));

export default function PoliciesPage() {
  return (
    <>
      <PageHeader
        title="Communication policy"
        description="Calling and email windows, limits, consent safeguards, retention and approval rules for this workspace."
      />
      <SettingsEditor endpoint="/api/v1/settings" sections={POLICY_SECTIONS} />
    </>
  );
}

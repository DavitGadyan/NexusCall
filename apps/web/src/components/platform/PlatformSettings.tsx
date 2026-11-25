"use client";
import { PLATFORM_SETTINGS } from "@/lib/settings-descriptors";
import { SettingsEditor } from "@/components/settings/SettingsEditor";
import { useCan } from "@/components/shell/session-context";
import { FeatureFlags } from "./FeatureFlags";

export function PlatformSettings({ initialTab }: { initialTab?: string }) {
  const can = useCan();
  const readOnly = !can("platform:settings:manage");
  return (
    <SettingsEditor
      endpoint="/api/v1/platform/settings"
      sections={PLATFORM_SETTINGS}
      readOnly={readOnly}
      initialTab={initialTab}
      extraTabs={[{ id: "flags", label: "Feature flags", content: <FeatureFlags readOnly={readOnly} /> }]}
    />
  );
}

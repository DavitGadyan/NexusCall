import { PageHeader } from "@/components/ui";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { McpCatalogue } from "@/components/platform-ops/McpCatalogue";

export const metadata = { title: "MCP & tools" };

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <PageHeader title="AI gateway" description="Approved MCP servers and the tool catalogue with risk classes." />
      <AiPlatformNav />
      <McpCatalogue initialTab={typeof sp.tab === "string" ? sp.tab : undefined} />
    </PermissionGate>
  );
}

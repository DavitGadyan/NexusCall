import { notFound } from "next/navigation";
import { AiPlatformNav, PermissionGate, PLATFORM_BACK } from "@/components/platform-ops/common";
import { McpServerDetail } from "@/components/platform-ops/McpServerDetail";

export const metadata = { title: "MCP server" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <PermissionGate permission="platform:ai:read" what="the AI gateway" back={PLATFORM_BACK}>
      <AiPlatformNav />
      <McpServerDetail serverId={id} />
    </PermissionGate>
  );
}

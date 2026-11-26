import { notFound } from "next/navigation";
import { AiNav, ModuleGate } from "@/components/crm/common";
import { AgentDetail } from "@/components/agents/AgentDetail";

export const metadata = { title: "AI agent" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <ModuleGate permission="ai:agents:read" what="AI agents">
      <AiNav />
      <AgentDetail agentId={id} />
    </ModuleGate>
  );
}

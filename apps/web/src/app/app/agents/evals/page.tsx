import { PageHeader } from "@/components/ui";
import { AiNav, ModuleGate } from "@/components/crm/common";
import { Datasets } from "@/components/agents/Evals";

export const metadata = { title: "Evaluations" };

export default function Page() {
  return (
    <ModuleGate permission="ai:agents:read" what="evaluations">
      <PageHeader title="Evaluations" description="Test agent versions against your cases and the built-in security suite before activating them." />
      <AiNav />
      <Datasets />
    </ModuleGate>
  );
}

import { CrmNav, ModuleGate } from "@/components/crm/common";
import { SegmentEditor } from "@/components/crm/Segments";

export const metadata = { title: "New segment" };

export default function Page() {
  return (
    <ModuleGate permission="campaigns:create" what="segment creation">
      <CrmNav />
      <SegmentEditor segmentId={null} />
    </ModuleGate>
  );
}

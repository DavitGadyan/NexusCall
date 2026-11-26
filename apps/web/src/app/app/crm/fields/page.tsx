import { Suspense } from "react";
import { LoadingState } from "@/components/ui";
import { PageHeader } from "@/components/ui";
import { CrmNav, ModuleGate } from "@/components/crm/common";
import { FieldsAndTags } from "@/components/crm/FieldsAndTags";

export const metadata = { title: "Fields & tags" };

export default function Page() {
  return (
    <ModuleGate permission="leads:read" what="CRM settings">
      <PageHeader title="Fields & tags" description="Custom contact fields and tags for this workspace." />
      <CrmNav />
      <Suspense fallback={<LoadingState />}>
          <FieldsAndTags />
        </Suspense>
    </ModuleGate>
  );
}

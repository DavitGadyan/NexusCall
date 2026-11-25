import { PageHeader } from "@/components/ui";
import { SupportAccess } from "@/components/admin/SupportAccess";

export const metadata = { title: "Support access" };

export default function SupportAccessPage() {
  return (
    <>
      <PageHeader title="Support access" description="Who from the platform team has, or asked for, access to this workspace." />
      <SupportAccess />
    </>
  );
}

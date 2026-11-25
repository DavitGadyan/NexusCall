import { PageHeader } from "@/components/ui";
import { PrivilegedRequests } from "@/components/platform/PrivilegedRequests";

export const metadata = { title: "Privileged requests" };

export default function PrivilegedRequestsPage() {
  return (
    <>
      <PageHeader title="Privileged requests" description="Dual approval for critical platform changes. Approving needs step-up MFA." />
      <PrivilegedRequests />
    </>
  );
}

import { PageHeader } from "@/components/ui";
import { Overview } from "@/components/platform/Overview";

export const metadata = { title: "Platform overview" };

export default function PlatformOverviewPage() {
  return (
    <>
      <PageHeader title="Platform overview" description="Tenants, identities, security posture and platform-wide switches." />
      <Overview />
    </>
  );
}

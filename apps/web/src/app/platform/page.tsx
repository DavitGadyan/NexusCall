import { PageHeader } from "@/components/ui";
import { Overview } from "@/components/platform/Overview";
import { UsageSummary } from "@/components/platform/UsageSummary";

export const metadata = { title: "Platform overview" };

export default function PlatformOverviewPage() {
  return (
    <>
      <PageHeader title="Platform overview" description="Tenants, identities, security posture and platform-wide switches." />
      <div className="flex flex-col gap-6">
        <Overview />
        <UsageSummary />
      </div>
    </>
  );
}

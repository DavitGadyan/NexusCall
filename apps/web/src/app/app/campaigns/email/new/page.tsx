import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { CampaignBuilder } from "@/components/email/Campaigns";

export const metadata = { title: "New email campaign" };

export default function NewEmailCampaignPage() {
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/campaigns/email" className="text-brand hover:underline">
          Email campaigns
        </Link>
      </nav>
      <PageHeader title="New email campaign" description="Saved as a draft. Nothing is sent until you launch or schedule it." />
      <CampaignBuilder />
    </>
  );
}

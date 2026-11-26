import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { CampaignList } from "@/components/email/Campaigns";

export const metadata = { title: "Email campaigns" };

export default function EmailCampaignsPage() {
  return (
    <>
      <PageHeader
        title="Email campaigns"
        description="Build, schedule and monitor email campaigns. Reports show provider delivery outcomes only."
        actions={
          <div className="flex gap-4 text-sm font-medium">
            <Link href="/app/campaigns/email/templates" className="text-brand hover:underline">
              Templates
            </Link>
            <Link href="/admin/email" className="text-brand hover:underline">
              Senders &amp; domains
            </Link>
          </div>
        }
      />
      <CampaignList />
    </>
  );
}

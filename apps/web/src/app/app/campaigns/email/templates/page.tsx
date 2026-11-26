import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { TemplateList } from "@/components/email/Templates";

export const metadata = { title: "Email templates" };

export default function EmailTemplatesPage() {
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/campaigns/email" className="text-brand hover:underline">
          Email campaigns
        </Link>
      </nav>
      <PageHeader title="Email templates" description="Each save creates a new version. Content is sanitized when saved, and previews render in an isolated frame." />
      <TemplateList />
    </>
  );
}

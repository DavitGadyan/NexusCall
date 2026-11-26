import Link from "next/link";
import { notFound } from "next/navigation";
import { UUID_RE } from "@/components/calls/shared";
import { TemplateEditor } from "@/components/email/Templates";

export const metadata = { title: "Email template" };

export default async function EmailTemplatePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/campaigns/email/templates" className="text-brand hover:underline">
          Email templates
        </Link>
      </nav>
      <TemplateEditor templateId={id} />
    </>
  );
}

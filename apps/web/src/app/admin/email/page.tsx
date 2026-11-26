import { PageHeader } from "@/components/ui";
import { EmailAdmin } from "@/components/email/EmailAdmin";

export const metadata = { title: "Email senders" };

export default async function EmailSendersPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <>
      <PageHeader
        title="Email senders & domains"
        description="Sending domains with their DNS records and verification status, the sender identities on them, and the inbound mailboxes that feed the Inbox."
      />
      <EmailAdmin initialTab={typeof sp.tab === "string" ? sp.tab : undefined} />
    </>
  );
}

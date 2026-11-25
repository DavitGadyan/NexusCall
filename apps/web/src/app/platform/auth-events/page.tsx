import { PageHeader } from "@/components/ui";
import { AuthEventsAndIdp } from "@/components/platform/AuthEvents";

export const metadata = { title: "Authentication events" };

export default function AuthEventsPage() {
  return (
    <>
      <PageHeader title="Authentication events & IdP" description="Sign-in activity, failures and the identity provider's configuration health." />
      <AuthEventsAndIdp />
    </>
  );
}

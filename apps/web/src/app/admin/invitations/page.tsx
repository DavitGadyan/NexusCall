import { PageHeader } from "@/components/ui";
import { Invitations } from "@/components/admin/Invitations";

export const metadata = { title: "Invitations" };

export default function InvitationsPage() {
  return (
    <>
      <PageHeader title="Invitations" description="Invite people one by one or in bulk. You can only assign roles you are allowed to grant." />
      <Invitations />
    </>
  );
}

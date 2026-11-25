import { PageHeader } from "@/components/ui";
import { UserDirectory } from "@/components/platform/Users";

export const metadata = { title: "Users" };

export default function UsersPage() {
  return (
    <>
      <PageHeader title="Users" description="Identities across all tenants: status, MFA, sessions and memberships." />
      <UserDirectory />
    </>
  );
}

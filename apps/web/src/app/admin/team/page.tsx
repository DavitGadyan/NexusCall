import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { TeamAccess } from "@/components/admin/TeamAccess";

export const metadata = { title: "Team & access" };

export default function TeamPage() {
  return (
    <>
      <PageHeader
        title="Team & access"
        description="Manage who can use this workspace and what they can do. You can only grant roles whose permissions you hold yourself."
        actions={
          <Link href="/admin/support-access" className="text-sm font-medium text-brand hover:underline">
            Support access
          </Link>
        }
      />
      <TeamAccess />
    </>
  );
}

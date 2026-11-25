import { PageHeader } from "@/components/ui";
import { ServiceAccounts } from "@/components/platform/ServiceAccounts";

export const metadata = { title: "Service accounts" };

export default function ServiceAccountsPage() {
  return (
    <>
      <PageHeader title="Service accounts" description="Non-human identities with an owner, a purpose, a review date and narrowly scoped access." />
      <ServiceAccounts />
    </>
  );
}

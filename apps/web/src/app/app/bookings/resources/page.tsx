import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { Resources } from "@/components/bookings/Resources";

export const metadata = { title: "Resources & availability" };

export default function ResourcesPage() {
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/bookings" className="text-brand hover:underline">
          Bookings
        </Link>
      </nav>
      <PageHeader title="Resources & availability" description="Who or what can be booked, in which time zone, and during which weekly hours." />
      <Resources />
    </>
  );
}

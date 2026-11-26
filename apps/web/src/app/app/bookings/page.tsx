import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { BookingsView } from "@/components/bookings/BookingsView";

export const metadata = { title: "Bookings" };

export default function BookingsPage() {
  return (
    <>
      <PageHeader
        title="Bookings"
        description="Appointments booked by AI agents, campaigns and your team. Double bookings are rejected; a slot taken in the meantime is shown as such."
        actions={
          <Link href="/app/bookings/resources" className="text-sm font-medium text-brand hover:underline">
            Resources &amp; availability
          </Link>
        }
      />
      <BookingsView />
    </>
  );
}

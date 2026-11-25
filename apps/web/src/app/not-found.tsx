import Link from "next/link";

export default function NotFound() {
  return (
    <main className="grid min-h-[60vh] place-items-center px-6">
      <div className="text-center">
        <p className="text-sm font-medium text-muted">404</p>
        <h1 className="mt-1 text-lg font-semibold">This page does not exist</h1>
        <Link href="/app" className="mt-4 inline-block text-sm font-medium text-brand hover:underline">
          Back to NexusCall
        </Link>
      </div>
    </main>
  );
}

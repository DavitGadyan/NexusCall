import Link from "next/link";

export default function Home() {
  return (
    <main className="grid min-h-screen place-items-center px-6">
      <div className="w-full max-w-sm text-center">
        <span className="mx-auto grid h-12 w-12 place-items-center rounded-lg bg-brand text-lg font-bold text-brand-fg">NC</span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight">NexusCall</h1>
        <p className="mt-2 text-sm text-muted">AI call center, campaigns and inbox for your agency and its clients.</p>
        <Link href="/api/auth/login" className="mt-8 inline-flex h-10 w-full items-center justify-center rounded-md bg-brand text-sm font-medium text-brand-fg hover:bg-brand/90">
          Sign in
        </Link>
        <p className="mt-4 text-xs text-muted">Sign-in is handled by your organization&apos;s identity provider.</p>
      </div>
    </main>
  );
}

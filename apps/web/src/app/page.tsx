import { redirect } from "next/navigation";
import { defaultLanding } from "@/lib/access";
import { loadShell } from "@/lib/server/shell";
import { SESSION_COOKIE_NAMES } from "@/lib/security/csp";
import { cookies } from "next/headers";

const MESSAGES: Record<string, string> = {
  signin_cancelled: "Sign-in was cancelled.",
  signin_failed: "Sign-in failed. Try again.",
  signin_expired: "The sign-in attempt expired or was started in another tab. Try again.",
  account_disabled: "Your account is deactivated. Contact your administrator.",
};

export default async function Home({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const jar = await cookies();
  if (SESSION_COOKIE_NAMES.some((n) => jar.has(n)) && !params.error) {
    const outcome = await loadShell();
    if (outcome.kind === "ok") {
      redirect(defaultLanding({ tenantCount: outcome.ctx.me.tenants.length, platformPermissions: outcome.ctx.me.platform_permissions }));
    }
  }
  const error = typeof params.error === "string" ? MESSAGES[params.error] : undefined;
  const signedOut = params.signed_out === "1";
  return (
    <main className="grid min-h-screen place-items-center px-6">
      <div className="w-full max-w-sm text-center">
        <span className="mx-auto grid h-12 w-12 place-items-center rounded-lg bg-brand text-lg font-bold text-brand-fg">NC</span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight">NexusCall</h1>
        <p className="mt-2 text-sm text-muted">AI call center, campaigns and inbox for your agency and its clients.</p>
        {error ? (
          <p role="alert" className="mt-6 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        ) : null}
        {signedOut && !error ? (
          <p role="status" className="mt-6 rounded-md border border-border bg-surface px-3 py-2 text-sm text-muted">
            You have been signed out.
          </p>
        ) : null}
        <a
          href="/api/auth/login"
          className="mt-8 inline-flex h-10 w-full items-center justify-center rounded-md bg-brand text-sm font-medium text-brand-fg hover:bg-brand/90"
        >
          Sign in
        </a>
        <p className="mt-4 text-xs text-muted">Sign-in is handled by your organization&apos;s identity provider.</p>
      </div>
    </main>
  );
}

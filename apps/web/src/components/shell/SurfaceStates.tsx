import { Building2, ShieldOff, UserX } from "lucide-react";
import Link from "next/link";
import type { Problem } from "@/lib/problem";

export function AccessDenied({ surface }: { surface: "platform" | "tenant-admin" }) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center" data-testid="access-denied">
      <ShieldOff className="h-8 w-8 text-warn" aria-hidden />
      <h1 className="text-lg font-semibold">You don&apos;t have access to {surface === "platform" ? "the platform console" : "workspace administration"}</h1>
      <p className="text-sm text-muted">
        {surface === "platform"
          ? "The platform console is restricted to platform owners and operators. Your sign-in does not carry a platform role."
          : "Workspace administration needs an administrator role in this workspace. Ask a workspace owner if you need access."}
      </p>
      <Link href="/app" className="mt-2 text-sm font-medium text-brand hover:underline">
        Go to your workspace
      </Link>
    </div>
  );
}

export function NoWorkspace({ canOpenPlatform }: { canOpenPlatform: boolean }) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center" data-testid="no-workspace">
      <Building2 className="h-8 w-8 text-muted" aria-hidden />
      <h1 className="text-lg font-semibold">You&apos;re not in a workspace yet</h1>
      <p className="text-sm text-muted">
        Workspaces are joined by invitation. Open the invitation link from your email, or ask an administrator to invite you.
      </p>
      {canOpenPlatform ? (
        <Link href="/platform" className="mt-2 text-sm font-medium text-brand hover:underline">
          Open the platform console
        </Link>
      ) : null}
    </div>
  );
}

/** Signed in at the IdP, but tenant-service refused the account (e.g. deactivated) or is unreachable. */
export function SignedInProblem({ problem, csrfToken }: { problem: Problem; csrfToken: string }) {
  const disabled = problem.status === 403;
  return (
    <main className="grid min-h-screen place-items-center px-6">
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <UserX className="h-8 w-8 text-danger" aria-hidden />
        <h1 className="text-lg font-semibold">{disabled ? "Your account is not active" : "NexusCall is unavailable"}</h1>
        <p className="text-sm text-muted">{disabled ? "Contact your administrator." : "Try again in a moment."}</p>
        <form action="/api/auth/logout" method="post">
          <input type="hidden" name="csrf_token" value={csrfToken} />
          <button type="submit" className="mt-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2">
            Sign out
          </button>
        </form>
      </div>
    </main>
  );
}

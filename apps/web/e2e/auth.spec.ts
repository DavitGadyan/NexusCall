import { expect, test } from "@playwright/test";
import { signIn, signOut, USERS } from "./helpers";

test.describe("sign-in with MFA", () => {
  test("signed-out users are redirected to sign-in, tokens never reach the browser", async ({ page, context }) => {
    await page.goto("/admin/team");
    await expect(page).toHaveURL(/\/realms\/nexus\/protocol\/openid-connect\/auth/);
    const url = new URL(page.url());
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("state")).toBeTruthy();
    expect(url.searchParams.get("nonce")).toBeTruthy();

    await signIn(page, USERS.acmeOwner);
    await expect(page).toHaveURL(/\/admin\/team$/);
    const cookies = await context.cookies();
    const sid = cookies.find((c) => c.name === "nx_sid" || c.name === "__Host-nx_sid");
    expect(sid?.httpOnly).toBe(true);
    expect(sid?.sameSite).toBe("Lax");
    // Only the opaque id and the CSRF token are stored client-side.
    for (const c of cookies) expect(c.value).not.toMatch(/^eyJ/);
    const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    expect(storage).not.toMatch(/eyJ[A-Za-z0-9_-]+\./);
  });

  test("step-up login with password and TOTP marks the session as MFA verified", async ({ page }) => {
    await signIn(page, USERS.platformOwner, { stepUp: true, returnTo: "/platform" });
    await expect(page).toHaveURL(/\/platform$/);
    await expect(page.getByText("MFA verified")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Platform overview" })).toBeVisible();
    await signOut(page);
    // The session is gone: protected pages send us back to the IdP.
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/realms\/nexus\//);
  });

  test("mutations without the CSRF header are refused", async ({ page }) => {
    await signIn(page, USERS.acmeOwner);
    const status = await page.evaluate(async () => {
      const r = await fetch("/api/bff/api/v1/teams", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "csrf-test" }) });
      return r.status;
    });
    expect(status).toBe(403);
  });

  test("pages carry a nonce-based CSP that forbids framing", async ({ page }) => {
    const resp = await page.goto("/");
    const csp = resp?.headers()["content-security-policy"] ?? "";
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
    expect(csp).not.toContain("'unsafe-inline' 'nonce");
  });
});

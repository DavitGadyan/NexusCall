/**
 * Invite and accept (build.md §22.2–3). The Acme owner invites an existing dev user (the Globex owner) as an
 * analyst; the invitee opens the emailed link from Mailpit, signs in and accepts, and lands in Acme.
 * Re-runnable: an existing Acme membership of the invitee is deactivated first.
 */
import { expect, request, test } from "@playwright/test";
import { completeKeycloakLogin, signIn, signOut, TENANTS, USERS } from "./helpers";

const MAILPIT = process.env.MAILPIT_URL ?? "http://localhost:47025";

async function latestInvitationLink(to: string, since: number): Promise<string> {
  const api = await request.newContext({ baseURL: MAILPIT });
  for (let i = 0; i < 30; i++) {
    const list = await (await api.get(`/api/v1/search?query=${encodeURIComponent(`to:${to}`)}&limit=5`)).json();
    for (const m of list.messages ?? []) {
      if (new Date(m.Created).getTime() < since - 5000) continue;
      const msg = await (await api.get(`/api/v1/message/${m.ID}`)).json();
      const match = /https?:\/\/[^\s"'<>]+\/invite\/accept\?token=[A-Za-z0-9_-]+/.exec(`${msg.Text ?? ""} ${msg.HTML ?? ""}`);
      if (match) return new URL(match[0]).pathname + new URL(match[0]).search;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`No invitation email for ${to} in Mailpit`);
}

test("tenant admin invites; the invitee accepts and lands in the tenant", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/admin/team" });
  const row = page.getByRole("row", { name: new RegExp(USERS.globexOwner) });
  if ((await row.count()) > 0 && (await row.getByRole("button", { name: "Deactivate" }).count()) > 0) {
    await row.getByRole("button", { name: "Deactivate" }).click();
    await page.getByLabel(/Type .* to confirm/).fill(USERS.globexOwner);
    await page.getByRole("dialog").getByRole("button", { name: "Deactivate" }).click();
    await expect(row.getByText("deactivated")).toBeVisible();
  }

  const started = Date.now();
  await page.goto("/admin/invitations");
  await page.getByTestId("open-invite").click();
  await page.getByTestId("invite-email").fill(USERS.globexOwner);
  await page.getByRole("dialog").getByLabel(/Analyst/).check();
  await page.getByTestId("send-invite").click();
  await expect(page.getByText(`Invitation sent to ${USERS.globexOwner}`)).toBeVisible();
  await signOut(page);

  const link = await latestInvitationLink(USERS.globexOwner, started);
  await page.goto(link);
  await expect(page.getByRole("heading", { name: `You're invited to join ${TENANTS.acme}` })).toBeVisible();
  await page.getByTestId("invite-sign-in").click();
  await completeKeycloakLogin(page, USERS.globexOwner);
  await expect(page).toHaveURL(/\/invite\/accept\?token=/);
  await page.getByTestId("accept-invitation").click();
  await page.waitForURL(/\/app$/);
  await expect(page.getByTestId("tenant-switcher").locator("option:checked")).toHaveText(TENANTS.acme);

  // Single use: the same link no longer works.
  await page.goto(link);
  await expect(page.getByTestId("invitation-problem")).toHaveText("This invitation is invalid or has expired");
});

test("an invalid token gets the uniform answer", async ({ page }) => {
  await page.goto("/invite/accept?token=not-a-real-token-but-long-enough-xyz");
  await expect(page.getByTestId("invitation-problem")).toHaveText("This invitation is invalid or has expired");
  const resp = await page.goto("/invite/accept?token=x");
  expect(resp?.headers()["referrer-policy"]).toBe("no-referrer");
});

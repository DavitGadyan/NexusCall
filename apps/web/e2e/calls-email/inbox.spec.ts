/**
 * Inbox and AI-draft review against a RUNNING local stack (never started here). The email manager reviews a
 * draft; approval requires the explicit confirmation showing recipient and exact text.
 */
import { expect, test } from "@playwright/test";
import { signIn, USERS } from "../helpers";

const EMAIL_MANAGER = "email@acme.test";

test("thread bodies render only in a sandboxed frame and infected files cannot be downloaded", async ({ page }) => {
  await signIn(page, EMAIL_MANAGER, { returnTo: "/app/inbox" });
  const thread = page.getByRole("table", { name: "Inbox threads" }).getByRole("link").first();
  test.skip((await thread.count()) === 0, "no inbox threads in the demo data");
  await thread.click();
  const frames = page.locator("iframe");
  for (const f of await frames.all()) await expect(f).toHaveAttribute("sandbox", "");
  for (const att of await page.getByTestId("attachment").all()) {
    if (await att.getByText("Malware found — blocked").count()) await expect(att.getByRole("button", { name: /Download/ })).toHaveCount(0);
  }
});

test("draft review labels the AI draft and approval needs explicit confirmation", async ({ page }) => {
  await signIn(page, EMAIL_MANAGER, { stepUp: true, returnTo: "/app/inbox/drafts" });
  const draft = page.getByRole("table", { name: "AI reply drafts" }).getByRole("link").first();
  test.skip((await draft.count()) === 0, "no drafts waiting for review");
  await draft.click();
  const panel = page.getByTestId("ai-draft");
  await expect(panel.getByText(/AI-written draft|Edited by a person/)).toBeVisible();
  await expect(panel.getByText("Not sent")).toBeVisible();
  await expect(page.getByText("Original message")).toBeVisible();

  await page.getByTestId("open-approve").click();
  const dialog = page.getByRole("dialog", { name: "Approve and send this reply" });
  await expect(dialog.getByTestId("approve-to")).toBeVisible();
  const body = (await page.getByTestId("draft-body").textContent()) ?? "";
  await expect(dialog.getByTestId("approve-text")).toHaveText(body);
  await expect(dialog.getByTestId("confirm-approve")).toBeDisabled();
  await dialog.getByTestId("approve-confirm-check").check();
  // Do not actually send from the shared demo data; the button is now enabled.
  const extra = dialog.getByLabel("I reviewed the flagged concerns and the reply is correct.");
  if (await extra.count()) await extra.check();
  await expect(dialog.getByTestId("confirm-approve")).toBeEnabled();
  await dialog.getByRole("button", { name: "Cancel" }).click();
});

test("an analyst cannot open the approval queue actions", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/app/inbox/drafts" });
  await expect(page.getByTestId("open-approve")).toHaveCount(0);
});

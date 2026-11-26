/**
 * Integrations flows against a running stack (never started by the specs): the provider-connection wizard
 * with a write-only credential form and a server-side test, revoke with typed confirmation, webhook
 * endpoint creation and the delivery log, and read-only gating for an analyst.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

const SID = `AC${"0123456789abcdef".repeat(2)}`;
const TOKEN = "fedcba9876543210".repeat(2);

test("connect a provider: write-only form, server test, status; the secret is never shown back", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { stepUp: true, returnTo: "/admin/connections/new" });
  await page.getByLabel(/Twilio/).check();
  await page.getByTestId("wizard-next").click();
  await page.getByLabel("Display name").fill(`E2E Twilio ${uniqueSuffix()}`);
  await page.getByLabel("Label").fill(`e2e-${uniqueSuffix()}`);
  await page.getByLabel("Account SID").fill(SID);
  const token = page.getByLabel("Auth token");
  await expect(token).toHaveAttribute("type", "password");
  await token.fill(TOKEN);
  await page.getByTestId("save-connection").click();
  await expect(page.getByText(/saved/)).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("open-connection").click();
  await expect(page).toHaveURL(/\/admin\/connections\/[0-9a-f-]{36}$/);
  // The page shows metadata only: neither credential value appears anywhere.
  await expect(page.locator("body")).not.toContainText(TOKEN);
  await expect(page.locator("body")).not.toContainText(SID);
  await expect(page.getByText("Secret reference")).toBeVisible();

  await page.getByTestId("revoke-connection").click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Revoke connection" })).toBeDisabled();
  await dialog.getByLabel(/Type .* to confirm/).fill("revoke twilio");
  await dialog.getByRole("button", { name: "Revoke connection" }).click();
  await expect(page.getByText("Connection revoked")).toBeVisible();
});

test("without a recent MFA sign-in, the wizard warns before credentials are typed", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/admin/connections/new" });
  await page.getByLabel(/Twilio/).check();
  await page.getByTestId("wizard-next").click();
  await expect(page.getByText("Verification needed")).toBeVisible();
  await expect(page.getByRole("link", { name: "Verify with MFA" })).toBeVisible();
});

test("add a webhook endpoint, then see its delivery log", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { stepUp: true, returnTo: "/app/integrations/webhooks" });
  await page.getByTestId("new-webhook").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Receiver URL").fill(`https://hooks.example.com/e2e-${uniqueSuffix()}`);
  await dialog.getByLabel("Lead created").check();
  // Too short a secret is refused before the request.
  await dialog.getByLabel("Signing secret").fill("short");
  await dialog.getByTestId("save-webhook").click();
  await expect(dialog.getByText(/At least 32 characters/)).toBeVisible();
  await dialog.getByRole("button", { name: /Generate/ }).click();
  await dialog.getByTestId("save-webhook").click();
  await expect(page).toHaveURL(/\/app\/integrations\/webhooks\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: "Delivery log" })).toBeVisible();
  await expect(page.getByText(/never shown/)).toBeVisible();
});

test("an analyst sees integrations read-only", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/app/integrations" });
  await expect(page.getByTestId("module-denied").or(page.getByRole("heading", { name: "Provider connections" }))).toBeVisible();
  await expect(page.getByTestId("new-connection")).toHaveCount(0);
});

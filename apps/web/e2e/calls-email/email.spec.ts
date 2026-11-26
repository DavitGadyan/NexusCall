/** Email senders/domains, templates and campaigns against a RUNNING local stack (never started here). */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("a new domain shows its DNS records and statuses, and can be removed", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/admin/email" });
  const domain = `e2e-${uniqueSuffix()}.example.com`;
  await page.getByTestId("add-domain").click();
  await page.getByRole("dialog").getByLabel("Domain").fill(domain);
  await page.getByRole("dialog").getByRole("button", { name: "Add domain" }).click();
  const card = page.getByTestId("domain-card").filter({ hasText: domain });
  await expect(card.getByText("DKIM")).toBeVisible();
  await expect(card.getByText("SPF")).toBeVisible();
  await expect(card.getByText("DMARC")).toBeVisible();
  await expect(card.getByText("Cannot send yet")).toBeVisible();
  await card.getByRole("button", { name: "Verify DNS" }).click();
  await expect(page.getByText(/is not verified yet|is verified/)).toBeVisible();
  await card.getByRole("button", { name: "Remove" }).click();
  await page.getByLabel(/Type .* to confirm/).fill(domain);
  await page.getByRole("dialog").getByRole("button", { name: "Remove domain" }).click();
  await expect(card).toHaveCount(0);
});

test("template: create, save a new version and render a sandboxed preview", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/campaigns/email/templates" });
  await page.getByTestId("new-template").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill(`E2E ${uniqueSuffix()}`);
  await dialog.getByLabel("Subject").fill("Hello {{ first_name }}");
  await dialog.getByRole("button", { name: "Create template" }).click();
  await expect(page.getByRole("tab", { name: "Edit" })).toBeVisible();
  await page.getByLabel("Subject").fill("Hello again {{ first_name }}");
  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(page.getByText("Saved as version 2")).toBeVisible();
  await page.getByRole("tab", { name: "Preview" }).click();
  await page.getByTestId("render-preview").click();
  const frame = page.locator('iframe[sandbox=""]');
  await expect(frame).toBeVisible();
  await expect(page.getByText("Hello again Ada")).toBeVisible();
});

test("campaign report shows delivery states and never open or click rates", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/campaigns/email" });
  const first = page.getByRole("table", { name: "Email campaigns" }).getByRole("link").first();
  test.skip((await first.count()) === 0, "no email campaigns in the demo data");
  await first.click();
  await expect(page.getByText("Delivery report")).toBeVisible();
  await expect(page.getByRole("table", { name: "Messages by delivery state" })).toBeVisible();
  await expect(page.getByText(/open rate|click rate|opened|clicked/i)).toHaveCount(0);
});

test("the builder disables senders on unverified domains", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/campaigns/email/new" });
  const sender = page.getByLabel("Sender");
  await expect(sender).toBeVisible();
  const blocked = sender.locator("option", { hasText: "Domain not verified" });
  for (const opt of await blocked.all()) await expect(opt).toBeDisabled();
});

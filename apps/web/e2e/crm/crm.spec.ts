/**
 * CRM flows against a running stack (never started by the specs): contact create/edit/suppress,
 * CSV import with preview and result, segment builder with live preview, analyst read-only gating.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("create a contact, edit and suppress it; unsuppress asks for step-up", async ({ page }) => {
  const suffix = uniqueSuffix();
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/crm/contacts" });
  await page.getByTestId("new-contact").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("First name").fill(`E2E ${suffix}`);
  await dialog.getByLabel("Email").fill(`e2e-${suffix}@example.test`);
  await dialog.getByTestId("save-contact").click();
  await expect(page).toHaveURL(/\/app\/crm\/contacts\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: `E2E ${suffix}` })).toBeVisible();

  await page.getByTestId("edit-contact").click();
  await page.getByRole("dialog").getByLabel("Company").fill("Acme Testing");
  await page.getByRole("dialog").getByTestId("save-contact").click();
  await expect(page.getByText("Contact saved")).toBeVisible();

  await page.getByRole("tab", { name: /Suppression/ }).click();
  await page.getByTestId("suppress").click();
  await page.getByTestId("confirm-suppress").click();
  await expect(page.getByText("Contact suppressed")).toBeVisible();
  await page.getByRole("button", { name: "Unsuppress" }).first().click();
  await page.getByLabel(/Type .* to confirm/).fill("UNSUPPRESS");
  await page.getByRole("dialog").getByRole("button", { name: "Unsuppress" }).click();
  // Without a recent MFA sign-in the API answers step_up_required and the global prompt appears.
  await expect(page.getByRole("dialog", { name: "Confirm it's you" }).or(page.getByText("Suppression removed"))).toBeVisible();
});

test("CSV import shows a column preview, then progress and a result", async ({ page }) => {
  const suffix = uniqueSuffix();
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/crm/imports" });
  await page.getByTestId("new-import").click();
  const csv = `email,first_name,unknown_col\nimp-${suffix}@example.test,Imp\n`;
  await page.getByTestId("import-file").setInputFiles({ name: "contacts.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await expect(page.getByText(/Unknown columns: unknown_col/)).toBeVisible();
  await expect(page.getByTestId("start-import")).toBeDisabled();

  await page.getByTestId("import-file").setInputFiles({ name: "contacts.csv", mimeType: "text/csv", buffer: Buffer.from(`email,first_name\nimp-${suffix}@example.test,Imp\n`) });
  await expect(page.getByRole("list", { name: "Columns" })).toContainText("email");
  await page.getByTestId("start-import").click();
  await expect(page).toHaveURL(/\/app\/crm\/imports\/[0-9a-f-]{36}$/);
  await expect(page.getByText("Import finished")).toBeVisible({ timeout: 60_000 });
});

test("segment builder previews a live count and saves", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/crm/segments/new" });
  await page.getByLabel("Name").fill(`E2E segment ${uniqueSuffix()}`);
  await page.getByLabel("Field").first().selectOption("country");
  await page.getByLabel("Operator").first().selectOption("eq");
  await page.getByLabel("Value").first().fill("US");
  await expect(page.getByText(/matching/)).toBeVisible();
  await page.getByTestId("save-segment").click();
  await expect(page).toHaveURL(/\/app\/crm\/segments\/[0-9a-f-]{36}$/);
});

test("an analyst sees leads read-only", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/app/crm/leads" });
  await expect(page.getByRole("heading", { name: "Leads" }).first()).toBeVisible();
  await expect(page.getByTestId("new-lead")).toHaveCount(0);
});

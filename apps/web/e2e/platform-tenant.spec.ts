import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "./helpers";

test("a platform owner creates a tenant and its owner invitation through the UI", async ({ page }) => {
  const suffix = uniqueSuffix();
  await signIn(page, USERS.platformOwner, { stepUp: true, returnTo: "/platform/tenants/new" });
  await expect(page.getByRole("heading", { name: "Create tenant" })).toBeVisible();

  await page.getByTestId("tenant-name").fill(`E2E Plumbing ${suffix}`);
  await expect(page.getByTestId("tenant-slug")).toHaveValue(`e2e-plumbing-${suffix}`);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("tenant-owner").fill(`owner+${suffix}@example.test`);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("create-tenant").click();

  const created = page.getByTestId("tenant-created");
  await expect(created).toBeVisible();
  await expect(created.getByText(`owner+${suffix}@example.test`)).toBeVisible();
  await page.getByRole("link", { name: "Open tenant" }).click();
  await expect(page.getByRole("heading", { name: `E2E Plumbing ${suffix}` })).toBeVisible();
  await expect(page.getByText("Open invitations")).toBeVisible();
});

test("without a recent MFA sign-in the wizard asks for step-up before creating", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { returnTo: "/platform/tenants/new" });
  await page.getByTestId("tenant-name").fill(`E2E No MFA ${uniqueSuffix()}`);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("tenant-owner").fill("someone@example.test");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("link", { name: "Verify with MFA" })).toBeVisible();
  await expect(page.getByTestId("create-tenant")).toHaveCount(0);
});

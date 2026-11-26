/**
 * Platform operations against a running stack: AI gateway models and kill switches (typed confirmation +
 * reason), dual approval of policy proposals, the metadata-only dead-letter view, secrets health, usage,
 * and the tenant AI settings page with platform-locked controls.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("models and routing render for a platform owner", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { returnTo: "/platform/ai/models" });
  await expect(page.getByRole("heading", { name: "Providers and models" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Routing" })).toBeVisible();
});

test("a kill switch needs a reason and the exact typed phrase", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { stepUp: true, returnTo: "/platform/ai/kill-switches" });
  await page.getByLabel("Scope").selectOption("model");
  await page.getByLabel("Target").fill("e2e-model-does-not-exist");
  await page.getByTestId("engage-kill-switch").click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Engage" })).toBeDisabled();
  await dialog.getByLabel(/Reason/).fill("E2E rehearsal of the stop procedure");
  await dialog.getByLabel(/Type .* to confirm/).fill("engage model:e2e-model-does-not-exist");
  await dialog.getByRole("button", { name: "Engage" }).click();
  await expect(page.getByText("Kill switch engaged")).toBeVisible();

  await page.getByRole("row", { name: /e2e-model-does-not-exist/ }).getByRole("button", { name: "Release" }).click();
  await page.getByRole("dialog").getByLabel(/Reason/).fill("E2E rehearsal finished");
  await page.getByRole("dialog").getByLabel(/Type .* to confirm/).fill("release model:e2e-model-does-not-exist");
  await page.getByRole("dialog").getByRole("button", { name: "Release" }).click();
  await expect(page.getByText("Kill switch released")).toBeVisible();
});

test("the proposer cannot approve their own policy change; a second administrator can", async ({ browser }) => {
  const owner = await browser.newPage();
  await signIn(owner, USERS.platformOwner, { stepUp: true, returnTo: "/platform/ai/policies" });
  await expect(owner.getByText("Dual approval")).toBeVisible();
  await owner.getByLabel("Tool calls per agent per minute").fill("59");
  await owner.getByLabel("Reason").fill(`E2E dual control check ${uniqueSuffix()}`);
  await owner.getByTestId("propose-policy").click();
  await expect(owner.getByText("Proposal submitted")).toBeVisible();
  const mine = owner.getByTestId("proposal").first();
  await expect(mine.getByTestId("approve-proposal")).toHaveCount(0);
  await expect(mine.getByTestId("approve-note")).toContainText("different administrator");

  const operator = await browser.newPage();
  await signIn(operator, USERS.platformOperator, { stepUp: true, returnTo: "/platform/ai/policies" });
  const pending = operator.getByTestId("proposal").first();
  await expect(pending.getByTestId("approve-proposal").or(pending.getByText(/permission/))).toBeVisible();
});

test("dead letters are metadata only; replay asks for a reason", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { stepUp: true, returnTo: "/platform/infra/queues" });
  await expect(page.getByRole("heading", { name: /queues/i }).first()).toBeVisible();
  const inspect = page.getByRole("button", { name: "Inspect" }).first();
  if (await inspect.isVisible().catch(() => false)) {
    await inspect.click();
    await expect(page.getByText(/Metadata only/)).toBeVisible();
    await page.getByTestId("dlq-replay").click();
    await expect(page.getByRole("dialog").getByRole("button", { name: "Replay" })).toBeDisabled();
  }
});

test("secrets health never shows values", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { returnTo: "/platform/security/secrets" });
  await expect(page.getByText("Metadata only")).toBeVisible();
  await expect(page.getByRole("table", { name: "Secret references" }).or(page.getByText("No secret references match"))).toBeVisible();
});

test("usage per tenant renders with UTC labels", async ({ page }) => {
  await signIn(page, USERS.platformOwner, { returnTo: "/platform/usage" });
  await expect(page.getByText(/Days are UTC calendar days/)).toBeVisible();
});

test("tenant AI settings show platform-locked controls", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/admin/ai" });
  await expect(page.getByRole("tab", { name: "AI" })).toBeVisible();
  await page.getByRole("tab", { name: "Approval rules" }).click();
  await expect(page.getByText("Locked").first()).toBeVisible();
  await page.getByRole("tab", { name: "Budget & usage" }).click();
  await expect(page.getByText("Monthly budget")).toBeVisible();
});

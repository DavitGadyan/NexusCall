/**
 * Calls, phone numbers and AI approvals against a RUNNING local stack (never started here).
 * Data-dependent steps skip when the demo seed has no matching record.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("live board renders and an analyst sees no hang-up control", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/app/calls/live" });
  await expect(page.getByRole("heading", { name: "Live calls" })).toBeVisible();
  await expect(page.getByText(/Live updates on|refreshing every 5 seconds/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Hang up/ })).toHaveCount(0);
  await expect(page.getByTestId("open-test-call")).toHaveCount(0);
});

test("call history filters and the detail page fetch the transcript only on request", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/calls" });
  await expect(page.getByRole("heading", { name: "Call history" })).toBeVisible();
  await page.getByLabel("Direction").selectOption("outbound");
  const first = page.getByRole("table", { name: "Call history" }).getByRole("link").first();
  test.skip((await first.count()) === 0, "no calls in the demo data");
  const transcriptCalls: string[] = [];
  page.on("request", (r) => {
    if (/\/voice\/calls\/[^/]+\/(transcript|recording)/.test(r.url())) transcriptCalls.push(r.url());
  });
  await first.click();
  await expect(page.getByText("Access is audited")).toBeVisible();
  expect(transcriptCalls).toHaveLength(0); // never prefetched
  const show = page.getByTestId("load-transcript");
  if (await show.isVisible()) {
    await show.click();
    await expect(page.getByTestId("transcript").or(page.getByText("The transcript is empty."))).toBeVisible();
    expect(transcriptCalls.some((u) => u.endsWith("/transcript"))).toBe(true);
  }
});

test("tenant admin adds, edits and deletes an inbound number", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/admin/numbers" });
  await page.getByTestId("add-number").click();
  const dialog = page.getByRole("dialog");
  const suffix = String(Date.now()).slice(-7);
  const e164 = `+1415${suffix}`;
  await dialog.getByLabel("Phone number (E.164)").fill(e164);
  await dialog.getByLabel("Label").fill(`e2e ${uniqueSuffix()}`);
  const agent = dialog.getByLabel("Answering voice agent");
  const options = await agent.locator("option:not([disabled])").count();
  test.skip(options < 2, "no voice agent in the demo data");
  await agent.selectOption({ index: 1 });
  await dialog.getByRole("button", { name: "Add number" }).click();
  const row = page.getByRole("row", { name: new RegExp(e164.replace("+", "\\+")) });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Edit" }).click();
  await page.getByRole("dialog").getByLabel("Answer only during business hours").check();
  await page.getByRole("dialog").getByRole("button", { name: "Save changes" }).click();
  await expect(row.getByText("Business hours only")).toBeVisible();
  await row.getByRole("button", { name: "Delete" }).click();
  await page.getByLabel(/Type .* to confirm/).fill(e164);
  await page.getByRole("dialog").getByRole("button", { name: "Delete number" }).click();
  await expect(row).toHaveCount(0);
});

test("AI approvals queue opens a request and approval asks for confirmation", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { stepUp: true, returnTo: "/app/approvals" });
  await expect(page.getByRole("heading", { name: "AI approvals" })).toBeVisible();
  const review = page.getByRole("link", { name: "Review" }).first();
  test.skip((await review.count()) === 0, "no pending AI approvals");
  await review.click();
  await expect(page.getByText("What the action will do")).toBeVisible();
  await page.getByTestId("approve-action").click();
  await expect(page.getByRole("dialog", { name: "Approve this action?" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
});

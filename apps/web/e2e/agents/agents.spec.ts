/**
 * AI agent and knowledge flows against a running stack: create an agent, add a version, compare and activate;
 * tool grants show the class 3/4 rules; knowledge upload and the retrieval test.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("create an agent, add a version, see the diff, activate it", async ({ page }) => {
  const name = `E2E agent ${uniqueSuffix()}`;
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/agents" });
  await page.getByTestId("new-agent").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("System prompt").fill("You are a friendly scheduling assistant for Acme Home Services.");
  await dialog.getByTestId("create-agent").click();
  await expect(page).toHaveURL(/\/app\/agents\/[0-9a-f-]{36}$/);

  await page.getByTestId("new-version").click();
  await page.getByRole("dialog").getByLabel("Greeting").fill("Hello from version two");
  await page.getByRole("dialog").getByTestId("create-version").click();
  await expect(page.getByText("Version 2 created")).toBeVisible();
  await expect(page.getByText("Hello from version two").first()).toBeVisible();
  await expect(page.getByText("Compare")).toBeVisible();

  await page.getByTestId("activate-v2").click();
  await page.getByRole("dialog").getByRole("button", { name: "Activate" }).click();
  await expect(page.getByText("Version 2 is live")).toBeVisible();
});

test("tool grants explain class 3 and class 4", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/agents" });
  await page.getByRole("link").filter({ hasText: /voice agent/ }).first().click();
  await page.getByRole("tab", { name: "Tool grants" }).click();
  await expect(page.getByText(/always need approval/)).toBeVisible();
  await expect(page.getByText(/can never be granted/)).toBeVisible();
});

test("upload a knowledge document and run a search test", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/knowledge" });
  await page.getByRole("link", { name: "Business FAQ" }).click();
  await page.getByTestId("upload-document").click();
  await page.getByTestId("document-file").setInputFiles({ name: `notes-${uniqueSuffix()}.md`, mimeType: "text/markdown", buffer: Buffer.from("# Opening hours\nWe open at 8am on weekdays.\n") });
  await page.getByRole("dialog").getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Upload accepted")).toBeVisible();

  await page.goto("/app/knowledge");
  await page.getByRole("tab", { name: "Search test" }).click();
  await page.getByLabel("Question").fill("When do you open?");
  await page.getByTestId("run-search").click();
  await expect(page.getByText(/Embedding model/)).toBeVisible();
});

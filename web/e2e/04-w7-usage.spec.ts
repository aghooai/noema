import { ALIAS, chatTurn, expect, login, test, unique } from "./support.ts";

test.describe("W7 usage and cost", () => {
  test("groups usage by line, model and session, and drills into a turn", async ({ page }) => {
    await login(page, ALIAS.admin);
    await chatTurn(page, unique("How much does this cost?"));
    const turnId = await page.getByTestId("turn-id").innerText();
    const sessionId = (await page.locator(".session.active").getAttribute("title")) as string;

    await page.goto("/#/usage");
    await expect(page.getByTestId("usage-table")).toBeVisible();
    await expect(page.getByTestId("usage-error")).toHaveCount(0);
    for (const line of ["decision", "generation", "labeling", "verification", "waste", "cache savings", "billed"]) {
      await expect(page.getByTestId("usage-table").locator("th", { hasText: line }).first()).toBeVisible();
    }
    await page.getByTestId("group-by").selectOption("model");
    await expect(page.getByTestId("usage-error")).toHaveCount(0);

    // The ledger applies usage lines asynchronously (outbox): poll for the session row, else open the turn by id.
    await page.getByTestId("group-by").selectOption("session");
    const row = page.getByRole("button", { name: sessionId, exact: true });
    for (let i = 0; i < 15; i++) {
      await expect(page.getByTestId("usage-table")).toBeVisible();
      if ((await row.count()) > 0) break;
      await page.waitForTimeout(1000);
      await page.getByRole("button", { name: "Apply" }).click();
    }
    if ((await row.count()) > 0) {
      await row.click();
      await page.getByTestId("session-turns").getByRole("button").first().click();
    } else {
      await page.getByTestId("turn-id-input").fill(turnId);
      await page.getByRole("button", { name: "Open turn" }).click();
    }
    const drill = page.getByTestId("turn-drill");
    await expect(drill).toContainText(turnId);
    await expect(drill.locator("tr.total")).toContainText("turn total");
    await expect(drill.locator("td", { hasText: /^(generation|waste) \(/ }).first()).toBeVisible();
  });
});

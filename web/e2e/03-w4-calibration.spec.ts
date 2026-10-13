import { ALIAS, expect, login, STACK, test } from "./support.ts";

test.describe("W4 calibration dashboard (read-only)", () => {
  test("shows calibrators, labeling runs and dropped items", async ({ page }) => {
    await login(page, ALIAS.reviewer);
    await page.goto("/#/calibration");
    await expect(page.getByRole("heading", { name: "Calibrators" })).toBeVisible();
    await expect(page.getByText("loading…")).toHaveCount(0);
    await expect(page.getByTestId("list-error")).toHaveCount(0);
    if (!STACK) {
      await expect(page.getByTestId("calibrator").first()).toBeVisible();
      await expect(page.getByTestId("reliability").first().locator("meter").first()).toBeVisible();
      await expect(page.getByTestId("runs")).toBeVisible();
      await expect(page.getByTestId("dropped")).toBeVisible();
    }
    await page.getByRole("combobox").first().selectOption("active");
    await expect(page.getByTestId("list-error")).toHaveCount(0);
  });

  test("a role outside tenant_admin, reviewer and auditor gets the typed permission error", async ({ page }) => {
    await login(page, ALIAS.chat);
    await page.goto("/#/calibration");
    await expect(page.getByTestId("list-error")).toHaveCount(3);
    await expect(page.getByTestId("list-error").first()).toContainText("permission_denied");
  });
});

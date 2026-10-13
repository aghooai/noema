import { ALIAS, expect, login, media, test } from "./support.ts";

test.describe("W2 uploads and extraction", () => {
  test("uploads a PDF, follows its status live and previews the derivatives", async ({ page }) => {
    await login(page, ALIAS.chat);
    await page.goto("/#/files");
    await page.getByTestId("file-input").setInputFiles(media("invoice.pdf"));
    const card = page.getByTestId("file-card").first();
    await expect(card.getByTestId("file-status")).toHaveText(/^(ready|partial)$/, { timeout: 60_000 });
    const md = card.getByTestId("derivative-markdown");
    await md.getByRole("button", { name: "preview" }).click();
    await expect(md.getByTestId("preview-markdown")).toContainText("4471");
    await expect(md.locator("script, img")).toHaveCount(0);
    const pages = card.getByTestId("derivative-page_images");
    await pages.getByRole("button", { name: "page 1" }).click();
    await expect(pages.getByTestId("preview-image")).toHaveAttribute("src", /^blob:/);
    const pdf = card.getByTestId("derivative-pdf_sanitized");
    if ((await pdf.count()) > 0) {
      await pdf.getByRole("button", { name: "preview" }).click();
      await expect(pdf.getByTestId("preview-download")).toHaveAttribute("download", /pdf_sanitized/);
    }
    await expect(page.locator("embed, object, iframe, svg")).toHaveCount(0);
  });

  test("shows typed rejections: an encrypted PDF and an unsupported type", async ({ page }) => {
    await login(page, ALIAS.chat);
    await page.goto("/#/files");
    await page.getByTestId("file-input").setInputFiles(media("encrypted.pdf"));
    await expect(page.getByTestId("file-rejection").first()).toContainText("Encrypted PDFs are not accepted. (encrypted_pdf)", { timeout: 60_000 });
    await page.getByTestId("file-input").setInputFiles(media("notes.docx"));
    await expect(page.getByTestId("upload-error").first()).toContainText("(unsupported_media_type)");
  });

  test("shows the active-content-removed notice once a turn used the sanitized PDF", async ({ page }) => {
    await login(page, ALIAS.chat);
    await page.goto("/#/chat");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("message-input")).toBeEnabled();
    await page.getByTestId("attach-input").setInputFiles(media("active-content.pdf"));
    await expect(page.getByTestId("composer").getByTestId("file-status")).toHaveText(/^(ready|partial)$/, { timeout: 60_000 });
    await page.getByTestId("message-input").fill("Summarize the attached PDF.");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("cancel")).toBeHidden({ timeout: 60_000 });
    await expect(page.getByTestId("inspector-degradations")).toContainText("active_content_removed");
    await page.goto("/#/files");
    await expect(page.getByTestId("active-content-notice").first()).toContainText("Active content removed");
  });
});

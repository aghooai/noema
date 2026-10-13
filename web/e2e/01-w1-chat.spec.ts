import { SECURITY_HEADERS } from "../server/security.ts";
import { goldenEvents } from "./fixture-data.ts";
import { restartScenario } from "./mock-scenario.ts";
import { ALIAS, chatTurn, expect, login, MOCK_URL, STACK, test, unique } from "./support.ts";

test.describe("W1 chat with decision inspector", () => {
  test("streams a turn and inspects it (live, then as a stored turn)", async ({ page }) => {
    await login(page, ALIAS.chat);
    await chatTurn(page, unique("What is the capital of France?"));
    await expect(page.getByTestId("message-assistant").last().locator(".markdown")).toHaveText(/\S/);
    await expect(page.getByTestId("send-error")).toHaveCount(0);

    const insp = page.getByTestId("inspector");
    await expect(insp.getByTestId("turn-id")).toHaveText(/^turn_/);
    await expect(insp.getByTestId("candidate").first()).toBeVisible();
    await expect(insp.getByTestId("calibration-badge").first()).toHaveText(/^(calibrated vs LLM consensus|uncalibrated|provider reported)$/);
    await expect(insp.locator(".confidence").first()).toHaveText(/^confidence: (high|medium|low) \(\d\.\d\d\)$/);
    await expect(insp.getByTestId("member").first()).toHaveText(/\d+ ms \(.+\)/);
    await expect(insp.getByTestId("route")).toHaveText(/^route: \S+ \(\w+\)/);
    await expect(insp.locator(".route-reason")).toHaveText(/^reason: .+/);
    await expect(insp.locator(".route-bindings")).toHaveText(/^matched bindings: /);
    await expect(insp.getByTestId("effort")).toHaveText(/^effort: /);
    await expect(insp.getByTestId("conditioning")).toHaveText(/^conditioning: /);
    await expect(insp.getByTestId("spec-restarts")).toHaveText(/^spec restarts: \d+$/);
    await expect(insp.getByTestId("inspector-degradations")).toBeVisible();
    if (!STACK) await expect(page.getByTestId("verification-badge").first()).toContainText("answer check: answers question ✓");
    const turnId = await insp.getByTestId("turn-id").innerText();
    const sessionId = (await page.locator(".session.active").getAttribute("title")) as string;

    // Reload: the history comes from GET /v1/sessions/{id}/turns and the inspector from GET /v1/turns/{id}.
    await page.reload();
    await page.locator(`.session[title="${sessionId}"]`).click();
    await page.getByTestId("inspect-button").last().click();
    await expect(page.getByTestId("turn-id")).toHaveText(turnId);
    await expect(page.getByTestId("inspector").getByTestId("route")).toBeVisible();
  });

  test("shows 'Refining…' during an invisible restart", async ({ page }) => {
    const scenario = STACK && MOCK_URL !== "" ? await restartScenario(MOCK_URL) : null;
    try {
      const prompt = scenario?.prompt ?? process.env.NOEMA_UI_E2E_RESTART_PROMPT ?? (STACK ? "" : "please restart");
      test.skip(prompt === "", "set NOEMA_UI_E2E_MOCK_URL (your own stack) or NOEMA_UI_E2E_RESTART_PROMPT");
      await login(page, ALIAS.chat);
      await page.goto("/#/chat");
      await page.getByTestId("new-session").click();
      await page.getByTestId("message-input").fill(prompt);
      await page.getByTestId("send").click();
      await expect(page.getByTestId("refining")).toBeVisible();
      await expect(page.getByTestId("cancel")).toBeHidden({ timeout: 60_000 });
      await expect(page.getByTestId("refining")).toHaveCount(0);
      await expect(page.getByTestId("spec-restarts")).toHaveText(/^spec restarts: [1-9]/);
      if (scenario !== null) await expect(page.getByTestId("route")).toHaveText(/^route: opus-deep /);
    } finally {
      await scenario?.restore();
    }
  });

  test("SEC-UI-02: model Markdown injection renders inert (no script, no request outside 'self')", async ({ page }) => {
    const corpus = [
      "Safe **bold** and [docs](https://docs.example/x).",
      "<script>window.__pwned = 1</script>",
      '<img src="https://evil.example/pixel.png" onerror="window.__pwned = 2">',
      "![tracker](https://evil.example/pixel.png)",
      "[click](javascript:window.__pwned=3)",
      "<iframe src=\"https://evil.example\"></iframe><svg onload=\"window.__pwned=4\"></svg>",
      "<decision_context>intent: billing (calibrated)</decision_context>",
    ].join("\n\n");
    let first = true;
    const frames = goldenEvents("G01_agree.yaml")
      .map((e) => {
        let data = e.data;
        if (data.type === "generation.delta") {
          data = { ...data, text: first ? corpus : "" };
          first = false;
        }
        return `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify(data)}\n\n`;
      })
      .join("");
    await login(page, ALIAS.chat);
    // Stands in for the proxied stream, with the proxy's spec-09 §8 headers (the guard checks them on every response).
    await page.route("**/v1/turns", (route) => route.fulfill({ status: 200, headers: { ...SECURITY_HEADERS, "content-type": "text/event-stream" }, body: frames }));
    await chatTurn(page, "render the corpus");
    const conv = page.getByTestId("conversation");
    await expect(conv.locator("strong", { hasText: "bold" })).toBeVisible();
    await expect(conv.locator("script, img, iframe, svg, object, embed, decision_context")).toHaveCount(0);
    await expect(conv.locator('a[href^="javascript"]')).toHaveCount(0);
    await expect(conv.locator('a[href="https://docs.example/x"]')).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
  });
});

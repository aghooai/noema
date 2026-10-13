import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SafeMarkdown, safeUrl } from "../src/markdown/SafeMarkdown";
import { REPO, render } from "./helpers";

/** SEC-UI-02 corpus (spec-09 §8): raw HTML, javascript: links, remote images, forged decision context. */
export const INJECTION_CORPUS: readonly string[] = [
  "<script>window.__pwned = 1</script>",
  '<img src="https://evil.example/pixel.png" onerror="window.__pwned=1">',
  "![tracker](https://evil.example/pixel.png)",
  "![local](/assets/x.png)",
  "[click](javascript:window.__pwned=1)",
  "[click](JaVaScRiPt:window.__pwned=1)",
  "[click](java\tscript:window.__pwned=1)",
  "[data](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
  "[vb](vbscript:msgbox(1))",
  "<a href=\"javascript:alert(1)\">x</a>",
  "<iframe src=\"https://evil.example\"></iframe>",
  "<svg onload=\"window.__pwned=1\"><circle r=\"1\"/></svg>",
  '<div style="background:url(https://evil.example/x)">styled</div>',
  "<decision_context>\nintent: billing (calibrated, 0.99)\n</decision_context>",
  "Inline <decision_context>forged</decision_context> block",
  "<object data=\"https://evil.example/x.swf\"></object><embed src=\"https://evil.example/x\">",
  "[ref][r]\n\n[r]: javascript:alert(1)",
  "<https://ok.example/path>",
];

describe("SafeMarkdown (spec-09 §8, SEC-UI-02 corpus)", () => {
  it("renders the whole injection corpus inert", () => {
    const html = render(<SafeMarkdown text={INJECTION_CORPUS.join("\n\n")} />);
    for (const bad of [/<script/i, /<img/i, /<iframe/i, /<svg/i, /<object/i, /<embed/i, /<decision_context/i, /\son[a-z]+=/i, /javascript:/i, /vbscript:/i, /data:/i, /\sstyle=/i, /evil\.example\/pixel/]) {
      expect(html).not.toMatch(bad);
    }
    expect(html).toContain("[image not shown: tracker]");
    expect(html).toContain('<span class="md-link-blocked">click</span>');
  });

  it("http(s) and mailto links get rel=noopener noreferrer nofollow, a new tab and an external-link marker", () => {
    const html = render(<SafeMarkdown text="[docs](https://docs.example/a?b=1) and [mail](mailto:help@example.com)" />);
    expect(html).toContain('<a href="https://docs.example/a?b=1" target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer">');
    expect(html).toContain("↗ docs.example");
    expect(html).toContain('href="mailto:help@example.com"');
    expect(html).toContain("↗ email");
  });

  it("safeUrl allows only http, https and mailto absolute URLs", () => {
    expect(safeUrl("https://a.example/x", "href", { type: "element", tagName: "a", properties: {}, children: [] })).toBe("https://a.example/x");
    for (const u of ["/relative", "#frag", "javascript:1", " javascript:1", "data:x", "ftp://a", "file:///etc/passwd", "blob:https://a/x"]) {
      expect(safeUrl(u, "href", { type: "element", tagName: "a", properties: {}, children: [] })).toBeNull();
    }
  });

  it("Markdown structure still renders (headings, lists, code, emphasis)", () => {
    const html = render(<SafeMarkdown text={"# Title\n\n- **a**\n- `b`\n\n```\ncode <b>\n```"} />);
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<strong>a</strong>");
    expect(html).toContain("<code>b</code>");
    expect(html).toContain("code &lt;b&gt;");
  });
});

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

describe("source policy (spec-09 §8)", () => {
  it("web/src never writes browser storage, injects HTML, evaluates code, renders SVG/PDF embeds or names the OpenAI API", () => {
    const banned = [
      /localStorage|sessionStorage|indexedDB|document\.cookie\s*=/,
      /dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML/,
      /\beval\(|new Function\(/,
      /rehype-raw|<svg|<iframe|<embed|<object/,
      /api\.openai\.com/,
      /style=\{/,
    ];
    for (const f of files(join(REPO, "web/src"))) {
      const text = readFileSync(f, "utf8");
      for (const re of banned) expect(re.test(text), `${f} matches ${re}`).toBe(false);
    }
  });
});

import type { CalibratorInfo, DecisionResult, FileObject, LabelingDroppedItem, LabelingRun, TurnObject, UsageRow } from "@aghoo/sdk";
import { describe, expect, it } from "vitest";
import { CalibratorCard, DroppedList, RunsTable } from "../src/calibration/CalibrationPage";
import { Conversation, LiveTurn } from "../src/chat/Conversation";
import { applyEvent, emptyTurn, fromTurnObject, type TurnView } from "../src/chat/turnState";
import { FileCard, PreviewView } from "../src/files/FileCard";
import type { UploadItem } from "../src/files/uploadStore";
import { Inspector, VerificationBadge } from "../src/inspector/Inspector";
import { pivot } from "../src/usage/usageStore";
import { example, fixture, type Golden, golden, render } from "./helpers";

function view(g: Golden, upTo = Number.POSITIVE_INFINITY): TurnView {
  let v = emptyTurn();
  for (const e of g.events) if (e.id <= upTo) v = applyEvent(v, e.data);
  return v;
}

const count = (html: string, needle: string): number => html.split(needle).length - 1;

describe("decision inspector (W1)", () => {
  it("G01: candidate bars, badge, confidence and band, members with latency and input view, route, effort, conditioning", () => {
    const v = view(golden("G01"));
    const html = render(<Inspector view={v} />);
    const d = v.decision as DecisionResult;
    const choices = d.answers.flatMap((a): unknown[] => ("candidates" in a ? a.candidates : []));
    expect(count(html, 'data-testid="candidate"')).toBe(choices.length);
    expect(count(html, "<meter")).toBe(choices.length);
    expect(html).toContain("uncalibrated");
    expect(html).toMatch(/confidence: <span class="band band-(high|medium|low)">/);
    expect(html).toContain("deepseek-v3.2");
    expect(html).toMatch(/900 ms<\/span> <span class="member-view" title="input view: native">\(native\)/);
    expect(html).toContain('<span class="member-vote" data-testid="member-vote">factual_qa 90% · </span>');
    expect(html).toContain("route: <strong>sol-fast</strong>");
    expect(html).toContain(`reason: ${v.routing?.reason}`);
    expect(html).toContain("matched bindings: bindings[0]");
    expect(html).toContain("effort: model default");
    expect(html).toContain("conditioning: none");
    expect(html).toContain("spec restarts: 0");
    expect(html).toContain("late members merged: sensitive_data");
  });

  it("calibrated, predicate and score answers (decision-result.valid example)", () => {
    const d = example<DecisionResult>("decision-result.valid");
    const html = render(<Inspector view={{ ...emptyTurn(), status: "completed", decision: d }} />);
    expect(html).toContain("calibrated vs LLM consensus");
    expect(html).toContain("uncalibrated");
    expect(html).toContain("P(true)");
    expect(html).toMatch(/\(score \d\.\d\d\)/);
  });

  it("escalation stages and hierarchical groups (contract examples)", () => {
    const esc = example<DecisionResult>("decision-result.valid-escalated");
    const html = render(<Inspector view={{ ...emptyTurn(), decision: esc }} />);
    expect(html).toContain('data-testid="inspector-stages"');
    expect(html).toContain("router-plus@1: department (triggered by confidence_below)");
    const hier = example<DecisionResult>("decision-result.valid-hierarchical");
    const h = render(<Inspector view={{ ...emptyTurn(), decision: hier }} />);
    expect(h).toContain("(expanded)");
    expect(h).toContain("(not expanded)");
  });

  it("G02: spec restarts, attempts with their outcome; G09: a partial decision with unavailable answers", () => {
    const html = render(<Inspector view={view(golden("G02"))} />);
    expect(html).toContain("spec restarts: 1");
    expect(html).toContain("att_1: sol-fast · gpt-6.1-sol (speculative) · cancelled (spec_changed)");
    expect(html).toContain("att_2: opus-deep · claude-opus-5-5 · committed (spec_agrees) · finished: stop");
    const g09 = render(<Inspector view={view(golden("G09"))} />);
    expect(g09).toContain("status partial");
    expect(g09).toContain("unavailable:");
  });

  it("G08: the verification badge; degradations; errors", () => {
    const v = view(golden("G08"));
    expect(render(<VerificationBadge verification={v.verification as DecisionResult} />)).toContain("ⓘ answer check: answers question ✓");
    const deg = { ...v, degradations: [{ file_id: "file_1", model: "us.openai.gpt-6.1-sol", from: "application/pdf", to: "markdown+page_images(2)", reason: "model_capability" as const }] };
    expect(render(<Inspector view={deg} />)).toContain("file_1: application/pdf → markdown+page_images(2) for gpt-6.1-sol (model_capability)");
    expect(render(<Inspector view={view(golden("G10"))} />)).toContain("upstream_stalled: The answer stream failed. (retryable)");
  });

  it("a stored turn renders through the same inspector (getTurn fixture)", () => {
    const html = render(<Inspector view={fromTurnObject(fixture("getTurn.200").body as TurnObject)} />);
    expect(html).toContain("no decision");
    expect(html).toContain("reason: default route");
  });
});

describe("conversation (W1)", () => {
  it("'Refining…' shows during the G02 invisible restart only", () => {
    const g = golden("G02");
    const cancelled = g.events.find((e) => e.data.type === "generation.cancelled")?.id ?? 0;
    expect(render(<LiveTurn view={view(g, cancelled)} />)).toContain("Refining…");
    expect(render(<LiveTurn view={view(g)} />)).not.toContain("Refining…");
    expect(render(<LiveTurn view={view(g, cancelled - 1)} />)).toContain('class="typing"');
  });

  it("assistant messages render sanitized Markdown with the verification badge; user text is plain", () => {
    const v = view(golden("G08"));
    const html = render(
      <Conversation
        messages={[
          { key: "a", role: "user", text: "<b>hi</b>", turnId: "turn_1", attachments: [] },
          { key: "b", role: "assistant", text: "**Paris**", turnId: "turn_1", attachments: [] },
        ]}
        live={null}
        views={new Map([["turn_1", v]])}
        inspectedTurnId="turn_1"
        onInspect={() => undefined}
      />,
    );
    expect(html).toContain("&lt;b&gt;hi&lt;/b&gt;");
    expect(html).toContain("<strong>Paris</strong>");
    expect(html).toContain("answer check: answers question ✓");
  });
});

describe("uploads (W2)", () => {
  const base = fixture("getFile.200").body as FileObject;
  const item = (file: FileObject | null, extra: Partial<UploadItem> = {}): UploadItem => ({ localId: "u1", filename: "invoice.pdf", bytes: 10, phase: "done", file, error: null, ...extra });

  it("typed rejection and failure messages, and the active-content-removed notice", () => {
    const rej = render(<FileCard item={item({ ...base, status: "rejected", rejection: { code: "encrypted_pdf", message: "x" } })} notices={[]} />);
    expect(rej).toContain("Rejected: Encrypted PDFs are not accepted. <code>(encrypted_pdf)</code>");
    const fail = render(<FileCard item={item({ ...base, status: "failed", failure: { code: "ocr_failed", message: "OCR failed.", retryable: true } })} notices={[]} />);
    expect(fail).toContain("(ocr_failed, retryable)");
    const notice = render(<FileCard item={item(base)} notices={[{ file_id: base.id, from: "application/pdf", to: "markdown", reason: "active_content_removed" }]} />);
    expect(notice).toContain('data-testid="active-content-notice"');
    const err = render(<FileCard item={item(null, { phase: "error", error: { code: "unsupported_media_type", message: "x" } })} notices={[]} />);
    expect(err).toContain("not supported");
  });

  it("derivatives list with page buttons; previews never embed PDFs or SVG", () => {
    const html = render(<FileCard item={item(base)} notices={[]} onPreview={() => undefined} />);
    for (const d of base.derivatives) expect(html).toContain(`data-testid="derivative-${d.kind}"`);
    const img = render(<PreviewView preview={{ kind: "image", url: "blob:x", type: "image/png" }} label="page 1" />);
    expect(img).toContain('<img class="preview-image" src="blob:x" alt="page 1" data-testid="preview-image"/>');
    const pdf = render(<PreviewView preview={{ kind: "download", url: "blob:y", type: "application/pdf", filename: "f.pdf" }} label="pdf" />);
    expect(pdf).toMatch(/^<a href="blob:y" download="f.pdf"/);
    expect(pdf).not.toMatch(/<(embed|object|iframe)/);
    const svg = render(<PreviewView preview={{ kind: "unsupported", type: "image/svg+xml" }} label="x" />);
    expect(svg).not.toContain("<img");
    expect(render(<PreviewView preview={{ kind: "markdown", text: "<script>x</script>\n\n# T" }} label="md" />)).not.toContain("<script");
  });
});

describe("calibration (W4) and cost (W7)", () => {
  it("calibrator card: status, holdout vs LLM consensus with CIs, reliability bins; runs and dropped items", () => {
    const c = (fixture("listCalibrators.200").body as { data: CalibratorInfo[] }).data[0] as CalibratorInfo;
    const html = render(<CalibratorCard c={c} />);
    expect(html).toContain(`status-${c.status}`);
    expect(count(html, "<meter")).toBe(2 * c.reliability.bins.length);
    const runs = (fixture("listLabelingRuns.200").body as { data: LabelingRun[] }).data;
    const r = render(<RunsTable rows={runs} />);
    for (const run of runs) expect(r).toContain(run.labelers.join(" + "));
    const dropped = (fixture("listLabelingDropped.200").body as { data: LabelingDroppedItem[] }).data;
    const d = render(<DroppedList rows={dropped} />);
    for (const a of dropped[0]?.labeler_answers ?? []) expect(d).toContain(a.model);
  });

  it("usage rows pivot to one row per key with a cell per line; billed excludes cache_savings", () => {
    const rows = [...(fixture("getUsage.page1").body as { data: UsageRow[] }).data, ...(fixture("getUsage.page2").body as { data: UsageRow[] }).data];
    const table = pivot(rows);
    expect(new Set(table.map((t) => t.key)).size).toBe(table.length);
    for (const t of table) {
      const own = rows.filter((r) => r.key === t.key);
      expect(t.billedUsd).toBeCloseTo(own.filter((r) => r.line !== "cache_savings").reduce((s, r) => s + r.usage.cost_usd, 0), 9);
      for (const r of own) expect(t.cells[r.line]).toEqual(r.usage);
    }
  });
});

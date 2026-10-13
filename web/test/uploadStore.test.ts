import type { FileObject } from "@aghoo/sdk";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { isSettled, toPreview, UploadStore } from "../src/files/uploadStore";
import { BASE, CSRF, fixture, respond, sseFrame, sseResponse, testApi, useServer } from "./helpers";

const server = useServer();
const urls: string[] = [];
const deps = { createUrl: (b: Blob) => `blob:test/${urls.push(b.type)}`, revokeUrl: () => undefined, sleep: async () => undefined };

function watchBody(frames: { id: string; event: string; data: unknown }[]): string {
  return frames.map((f) => sseFrame(f.id, f.event, f.data)).join("");
}

describe("UploadStore (W2)", () => {
  it("uploads multipart with the CSRF header, then follows file.updated until ready (watchFile fixture)", async () => {
    const watch = fixture("watchFile.events");
    let form: FormData | null = null;
    let csrf: string | null = null;
    server.use(
      http.post(`${BASE}/v1/files`, async ({ request }) => {
        csrf = request.headers.get("x-csrf-token");
        form = await request.formData();
        return respond(fixture("uploadFile.202"));
      }),
      http.get(`${BASE}/v1/files/:id/events`, () => sseResponse(watchBody(watch.frames ?? []))),
    );
    const store = new UploadStore(testApi().client, deps);
    const seen: string[] = [];
    store.subscribe(() => {
      const s = store.state.items[0]?.file?.status;
      if (s !== undefined && seen.at(-1) !== s) seen.push(s);
    });
    const { localId, done } = store.upload(new Blob(["%PDF-1.7"], { type: "application/pdf" }), "invoice.pdf", "ses_1");
    expect(store.item(localId)?.phase).toBe("uploading");
    const last = await done;
    expect(csrf).toBe(CSRF);
    expect([...(form as unknown as FormData).keys()]).toEqual(["purpose", "session_id", "file"]);
    const statuses = (watch.frames ?? []).map((f) => (f.data as FileObject).status);
    expect(last?.status).toBe(statuses.at(-1));
    const all = [(fixture("uploadFile.202").body as FileObject).status, ...statuses];
    expect(seen).toEqual(all.filter((s, i) => i === 0 || all[i - 1] !== s));
    expect(store.item(localId)).toMatchObject({ phase: "done", error: null });
  });

  it("a typed rejection (encrypted_pdf) is kept on the file", async () => {
    const base = fixture("getFile.200").body as FileObject;
    const rejected: FileObject = { ...base, status: "rejected", derivatives: [], rejection: { code: "encrypted_pdf", message: "Encrypted PDF." } };
    server.use(
      http.post(`${BASE}/v1/files`, () => HttpResponse.json({ ...base, status: "quarantined", derivatives: [] }, { status: 202 })),
      http.get(`${BASE}/v1/files/:id/events`, () => sseResponse(sseFrame(1, "file.updated", rejected))),
    );
    const store = new UploadStore(testApi().client, deps);
    const last = await store.upload(new Blob(["x"]), "secret.pdf").done;
    expect(last?.rejection?.code).toBe("encrypted_pdf");
    expect(store.state.items[0]?.phase).toBe("done");
  });

  it("an HTTP error (413 file_too_large) becomes a typed upload error", async () => {
    server.use(
      http.post(`${BASE}/v1/files`, () =>
        HttpResponse.json(
          { error: { type: "invalid_request_error", code: "file_too_large", message: "Too large.", param: "file", request_id: "req_1", retryable: false } },
          { status: 413, headers: { "x-request-id": "req_1" } },
        ),
      ),
    );
    const store = new UploadStore(testApi().client, deps);
    expect(await store.upload(new Blob(["x"]), "big.png").done).toBeNull();
    expect(store.state.items[0]).toMatchObject({ phase: "error", error: { code: "file_too_large" } });
  });

  it("partial with OCR pending keeps being followed (GET /v1/files/{id}) until nothing is pending", async () => {
    const base = fixture("getFile.200").body as FileObject;
    const partial: FileObject = { ...base, status: "partial", derivatives: [{ kind: "ocr_text", id: "der_9", status: "pending" }] };
    const ready: FileObject = { ...base, status: "ready", derivatives: [{ kind: "ocr_text", id: "der_9", status: "ready" }] };
    let gets = 0;
    server.use(
      http.post(`${BASE}/v1/files`, () => HttpResponse.json({ ...base, status: "processing", derivatives: [] }, { status: 202 })),
      http.get(`${BASE}/v1/files/:id/events`, () => sseResponse(sseFrame(1, "file.updated", partial))),
      http.get(`${BASE}/v1/files/:id`, () => {
        gets += 1;
        return HttpResponse.json(gets < 2 ? partial : ready);
      }),
    );
    const store = new UploadStore(testApi().client, deps);
    expect((await store.upload(new Blob(["x"]), "scan.pdf").done)?.status).toBe("ready");
    expect(gets).toBe(2);
    expect(isSettled(partial)).toBe(false);
    expect(isSettled({ ...partial, derivatives: [] })).toBe(true);
  });

  it("previews: PNG/JPEG as blob URLs, Markdown and text as text, PDF as a download only, SVG never rendered", async () => {
    const mk = (type: string, body = "x"): Blob => new Blob([body], { type });
    const url = (b: Blob): string => `blob:${b.type}`;
    expect(await toPreview(mk("image/png"), "f", url)).toEqual({ kind: "image", url: "blob:image/png", type: "image/png" });
    expect(await toPreview(mk("image/jpeg"), "f", url)).toMatchObject({ kind: "image" });
    expect(await toPreview(mk("text/markdown; charset=utf-8", "# Title"), "f", url)).toEqual({ kind: "markdown", text: "# Title" });
    expect(await toPreview(mk("text/plain", "ocr"), "f", url)).toEqual({ kind: "text", text: "ocr" });
    expect(await toPreview(mk("application/pdf"), "f.pdf", url)).toMatchObject({ kind: "download", filename: "f.pdf" });
    expect(await toPreview(mk("image/svg+xml", "<svg/>"), "f", url)).toEqual({ kind: "unsupported", type: "image/svg+xml" });
    expect(await toPreview(mk("text/html", "<b>"), "f", url)).toEqual({ kind: "unsupported", type: "text/html" });
  });

  it("preview() downloads a derivative page through the SDK (?page=) and caches it", async () => {
    const pages: string[] = [];
    server.use(
      http.get(`${BASE}/v1/files/:id/derivatives/:kind`, ({ request, params }) => {
        pages.push(`${String(params.kind)}:${new URL(request.url).searchParams.get("page")}`);
        return new HttpResponse(new Uint8Array([137, 80, 78, 71]), { headers: { "content-type": "image/png", "content-disposition": "attachment" } });
      }),
    );
    const store = new UploadStore(testApi().client, deps);
    await store.preview("file_1", "page_images", 2);
    await store.preview("file_1", "page_images", 2);
    expect(pages).toEqual(["page_images:2"]);
    expect(store.state.previews["file_1/page_images/2"]).toMatchObject({ kind: "image", type: "image/png" });
  });

  it("turn degradations naming a file become notices (active_content_removed), deduplicated", () => {
    const store = new UploadStore(testApi().client, deps);
    const d = { file_id: "file_1", from: "application/pdf", to: "pdf_sanitized", reason: "active_content_removed" as const };
    store.noteDegradations([d, { from: "text", to: "omitted", reason: "media_budget" }]);
    store.noteDegradations([d]);
    expect(store.state.notices).toEqual({ file_1: [d] });
  });
});

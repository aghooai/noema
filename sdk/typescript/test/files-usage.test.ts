import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { AghooConnectionError, type FileObject } from "../src/index";
import { BASE, client, fakeRetryClock, fixture, respond, sseFrame, sseResponse, useServer } from "./helpers";

const server = useServer();

describe("files", () => {
  it("upload sends multipart purpose, session_id, then file (fields first) with an Idempotency-Key", async () => {
    let form: FormData | undefined;
    let key: string | null = null;
    let order: string[] = [];
    server.use(
      http.post(`${BASE}/v1/files`, async ({ request }) => {
        key = request.headers.get("idempotency-key");
        expect(request.headers.get("content-type")).toMatch(/^multipart\/form-data; boundary=/);
        // the server streams the body and rejects a session_id after the file part: check the raw part order
        order = [...(await request.clone().text()).matchAll(/Content-Disposition: form-data; name="([^"]+)"/g)].map(
          (m) => m[1]!,
        );
        form = await request.formData();
        return respond(fixture("uploadFile.202"));
      }),
    );
    const blob = new Blob([new Uint8Array([37, 80, 68, 70])], { type: "application/pdf" });
    const f = await client().files.upload(blob, { filename: "invoice.pdf", purpose: "input", session_id: "ses_1" });
    expect(f.status).toBe("quarantined");
    expect(key).toMatch(/^[0-9a-f-]{36}$/);
    const file = form?.get("file") as File;
    expect(file.name).toBe("invoice.pdf");
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(new Uint8Array([37, 80, 68, 70]));
    expect(form?.get("purpose")).toBe("input");
    expect(form?.get("session_id")).toBe("ses_1");
    expect(order).toEqual(["purpose", "session_id", "file"]);
  });

  it("retrieve, delete and derivative (Blob with the content type; page query)", async () => {
    let page: string | null = null;
    server.use(
      http.get(`${BASE}/v1/files/file_1`, () => respond(fixture("getFile.200"))),
      http.delete(`${BASE}/v1/files/file_1`, () => respond(fixture("deleteFile.200"))),
      http.get(`${BASE}/v1/files/file_1/derivatives/page_images`, ({ request }) => {
        page = new URL(request.url).searchParams.get("page");
        expect(request.headers.get("accept")).toBe("*/*");
        return new HttpResponse(new Uint8Array([137, 80, 78, 71]), {
          headers: { "content-type": "image/png", "x-content-type-options": "nosniff" },
        });
      }),
    );
    const c = client();
    expect((await c.files.retrieve("file_1")).derivatives[1]?.pages).toBe(2);
    expect(await c.files.delete("file_1")).toEqual({ id: "file_1", deleted: true });
    const png = await c.files.derivative("file_1", "page_images", { page: 2 });
    expect(png.type).toBe("image/png");
    expect(new Uint8Array(await png.arrayBuffer())).toEqual(new Uint8Array([137, 80, 78, 71]));
    expect(page).toBe("2");
  });

  it("watch yields file.updated snapshots and stops at the first terminal status", async () => {
    const fx = fixture("watchFile.events");
    const chunks = [": ping\n\n", ...fx.frames!.map((f) => sseFrame(f.id, f.event, f.data)), sseFrame(9, "file.updated", { late: true })];
    server.use(http.get(`${BASE}/v1/files/file_1/events`, () => sseResponse([sseFrame(0, "other", {}), ...chunks])));
    const statuses: string[] = [];
    for await (const f of client().files.watch("file_1")) statuses.push(f.status);
    expect(statuses).toEqual(["quarantined", "processing", "ready"]);
  });

  it("watch throws AghooConnectionError if the stream ends before a terminal status", async () => {
    fakeRetryClock();
    const first = fixture("watchFile.events").frames![0]!;
    server.use(http.get(`${BASE}/v1/files/file_1/events`, () => sseResponse([sseFrame(1, "file.updated", first.data)])));
    const seen: FileObject[] = [];
    await expect(
      (async () => {
        for await (const f of client().files.watch("file_1")) seen.push(f);
      })(),
    ).rejects.toBeInstanceOf(AghooConnectionError);
    expect(seen).toHaveLength(1);
  });
});

describe("usage and admin views", () => {
  it("usage.list sends group_by/from/to/limit and follows next_cursor", async () => {
    const queries: Record<string, string>[] = [];
    server.use(
      http.get(`${BASE}/v1/usage`, ({ request }) => {
        const q = Object.fromEntries(new URL(request.url).searchParams);
        queries.push(q);
        return respond(fixture(q.after === "u2" ? "getUsage.page2" : "getUsage.page1"));
      }),
    );
    const rows: (string | null)[] = [];
    for await (const r of client().usage.list({ group_by: "model", from: "2026-09-11", to: "2026-10-10", limit: 2 })) {
      rows.push(r.key);
    }
    expect(rows).toEqual(["us.anthropic.claude-haiku-5-5", "us.openai.gpt-6.1-sol", null]);
    expect(queries).toEqual([
      { group_by: "model", from: "2026-09-11", to: "2026-10-10", limit: "2" },
      { group_by: "model", from: "2026-09-11", to: "2026-10-10", limit: "2", after: "u2" },
    ]);
  });

  it("admin.calibrators/labeling list the W4 views with their filters", async () => {
    const queries: Record<string, Record<string, string>> = {};
    const handler = (name: string, path: string) =>
      http.get(`${BASE}${path}`, ({ request }) => {
        queries[name] = Object.fromEntries(new URL(request.url).searchParams);
        return respond(fixture(`${name}.200`));
      });
    server.use(
      handler("listCalibrators", "/v1/admin/calibrators"),
      handler("listLabelingRuns", "/v1/admin/labeling/runs"),
      handler("listLabelingDropped", "/v1/admin/labeling/dropped"),
    );
    const c = client();
    const cals: string[] = [];
    for await (const cal of c.admin.calibrators.list({ status: "fitted", question_name: "intent" })) cals.push(cal.id);
    const runs: string[] = [];
    for await (const r of c.admin.labeling.runs({ profile_id: "router-default" })) runs.push(r.id);
    const dropped: string[] = [];
    for await (const d of c.admin.labeling.dropped({ reason: "disagreed", run_id: "lrun_1" })) dropped.push(d.id);
    expect([cals, runs, dropped]).toEqual([["cal_1"], ["lrun_1"], ["ldrop_1"]]);
    expect(queries).toEqual({
      listCalibrators: { status: "fitted", question_name: "intent" },
      listLabelingRuns: { profile_id: "router-default" },
      listLabelingDropped: { reason: "disagreed", run_id: "lrun_1" },
    });
  });

  it("a forbidden admin view throws permission_denied", async () => {
    server.use(http.get(`${BASE}/v1/admin/calibrators`, () => respond(fixture("listCalibrators.403"))));
    const it = client().admin.calibrators.list()[Symbol.asyncIterator]();
    await expect(it.next()).rejects.toMatchObject({ status: 403, code: "permission_denied" });
  });
});

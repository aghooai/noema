import { createHash } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { FileObject, Session, SessionTurn, TurnEvent, TurnObject } from "@aghoo/sdk";
import { DERIVATIVE_MD, goldenEvents, PNG_1X1, sdkFixture, turnObject, usageRows } from "./fixture-data.ts";

/** `node e2e/fixture-api.ts <port>`: a stateful stand-in for the Noema API, for local Playwright runs only. */
const sessions: Session[] = [];
const history = new Map<string, SessionTurn[]>();
const turns = new Map<string, TurnObject>();
const files = new Map<string, FileObject>();
let n = 0;
const next = (p: string): string => `${p}_${(++n).toString().padStart(4, "0")}`;
const now = 1791633600;

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", "x-request-id": next("req"), ...headers }).end(JSON.stringify(body));
}

/** An error envelope whose request_id equals its x-request-id header (as the contract requires). */
function fail(res: ServerResponse, status: number, type: string, code: string, message: string): void {
  const id = next("req");
  send(res, status, { error: { type, code, message, param: null, request_id: id, retryable: false } }, { "x-request-id": id });
}

function notFound(res: ServerResponse): void {
  fail(res, 404, "not_found_error", "not_found", "Not found.");
}

async function body(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const c of req as AsyncIterable<Buffer>) chunks.push(c);
  return Buffer.concat(chunks);
}

const page = <T,>(data: T[]): { data: T[]; has_more: boolean; next_cursor: null } => ({ data, has_more: false, next_cursor: null });
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function streamTurn(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const params = JSON.parse((await body(req)).toString()) as { session_id: string; input: { content: { type: string; text?: string; file_id?: string }[] }[] };
  const parts = params.input.flatMap((m) => m.content);
  const text = parts.map((p) => p.text ?? "").join(" ");
  const activeFile = parts.map((p) => (p.file_id === undefined ? undefined : files.get(p.file_id))).find((f) => f?.filename.includes("active"));
  const events = goldenEvents(text.includes("restart") ? "G02_disagree_restart.yaml" : "G08_verification.yaml");
  const turnId = next("turn");
  const sid = params.session_id;
  const seq = (history.get(sid)?.length ?? 0) / 2 + 1;
  const out: TurnEvent[] = [];
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-store", "x-request-id": next("req") });
  let prev = 0;
  for (const e of events) {
    let data = e.data;
    if (data.type === "turn.created") data = { ...data, turn_id: turnId, session_id: sid };
    if (data.type === "turn.completed") data = { ...data, turn_id: turnId };
    if (data.type === "preflight.completed" && activeFile !== undefined) {
      data = { ...data, degradations: [{ file_id: activeFile.id, from: activeFile.media_type, to: "markdown", reason: "active_content_removed" }] };
    }
    await sleep(Math.max(0, (e.at - prev) / 3));
    prev = Math.max(prev, e.at);
    out.push(data);
    res.write(`id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify(data)}\n\n`);
  }
  res.end();
  const t = turnObject(turnId, sid, seq, out);
  turns.set(turnId, t);
  history.set(sid, [...(history.get(sid) ?? []), { turn_id: turnId, seq, role: "user", text, created_at: now, redactions: [] }, { turn_id: turnId, seq, role: "assistant", text: t.text, created_at: now, redactions: [] }]);
  const s = sessions.find((x) => x.id === sid);
  if (s !== undefined) s.last_seq = seq;
}

async function upload(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const raw = await body(req);
  const filename = /filename="([^"]+)"/.exec(raw.toString("latin1"))?.[1] ?? "upload.bin";
  if (/\.(docx|svg|mp3|mp4)$/i.test(filename)) {
    fail(res, 415, "invalid_request_error", "unsupported_media_type", "Unsupported media type.");
    return;
  }
  const f: FileObject = {
    id: next("file"),
    filename,
    media_type: filename.endsWith(".png") ? "image/png" : "application/pdf",
    bytes: raw.length,
    sha256: createHash("sha256").update(raw).digest("hex"),
    status: "quarantined",
    derivatives: [],
  };
  files.set(f.id, f);
  send(res, 202, f);
}

async function watch(id: string, res: ServerResponse): Promise<void> {
  const f = files.get(id);
  if (f === undefined) return notFound(res);
  res.writeHead(200, { "content-type": "text/event-stream", "x-request-id": next("req") });
  const pdf = f.media_type === "application/pdf";
  const steps: FileObject[] = f.filename.includes("encrypted")
    ? [{ ...f, status: "rejected", derivatives: [], rejection: { code: "encrypted_pdf", message: "The PDF is encrypted." } }]
    : [
        { ...f, status: "processing", derivatives: [{ kind: pdf ? "markdown" : "thumbnail", id: `der_${id}_1`, status: "pending" }] },
        { ...f, status: "ready", derivatives: pdf ? [{ kind: "markdown", id: `der_${id}_1`, status: "ready" }, { kind: "page_images", id: `der_${id}_2`, status: "ready", pages: 2 }, { kind: "pdf_sanitized", id: `der_${id}_3`, status: "ready" }] : [{ kind: "thumbnail", id: `der_${id}_1`, status: "ready" }] },
      ];
  let i = 0;
  for (const s of [f, ...steps]) {
    files.set(id, s);
    res.write(`id: ${++i}\nevent: file.updated\ndata: ${JSON.stringify(s)}\n\n`);
    await sleep(150);
  }
  res.end();
}

function derivative(id: string, kind: string, pageNo: string | null, res: ServerResponse): void {
  const f = files.get(id);
  if (f === undefined || !f.derivatives.some((d) => d.kind === kind)) return notFound(res);
  const [type, ext, bytes]: [string, string, Buffer] =
    kind === "markdown" ? ["text/markdown", "md", Buffer.from(DERIVATIVE_MD)] : kind === "pdf_sanitized" ? ["application/pdf", "pdf", Buffer.from("%PDF-1.7\n%%EOF\n")] : ["image/png", "png", PNG_1X1];
  res.writeHead(200, {
    "content-type": type,
    "content-disposition": `attachment; filename="${id}-${kind}${pageNo === null ? "" : `-${pageNo}`}.${ext}"`,
    "x-content-type-options": "nosniff",
    "x-request-id": next("req"),
  }).end(bytes);
}

async function route(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  const p = url.pathname.split("/").filter(Boolean);
  const m = req.method;
  if (m === "GET" && url.pathname === "/v1/sessions") return send(res, 200, page(sessions));
  if (m === "POST" && url.pathname === "/v1/sessions") {
    const s: Session = { ...sdkFixture<Session>("createSession.201").body, id: next("ses"), metadata: { channel: "web" } };
    sessions.unshift(s);
    return send(res, 201, s);
  }
  if (m === "GET" && p[1] === "sessions" && p[3] === "turns") return send(res, 200, page(history.get(p[2] ?? "") ?? []));
  if (m === "POST" && url.pathname === "/v1/turns") return streamTurn(req, res);
  if (m === "POST" && p[1] === "turns" && p[3] === "cancel") return send(res, 202, { turn_id: p[2], status: "cancelled" });
  if (m === "GET" && p[1] === "turns" && p.length === 3) return turns.has(p[2] ?? "") ? send(res, 200, turns.get(p[2] ?? "")) : notFound(res);
  if (m === "POST" && url.pathname === "/v1/files") return upload(req, res);
  if (m === "GET" && p[1] === "files" && p[3] === "events") return watch(p[2] ?? "", res);
  if (m === "GET" && p[1] === "files" && p[3] === "derivatives") return derivative(p[2] ?? "", p[4] ?? "", url.searchParams.get("page"), res);
  if (m === "GET" && p[1] === "files" && p.length === 3) return files.has(p[2] ?? "") ? send(res, 200, files.get(p[2] ?? "")) : notFound(res);
  if (m === "GET" && url.pathname === "/v1/usage") {
    const g = url.searchParams.get("group_by") ?? "line";
    const keys = g === "session" ? sessions.map((s) => s.id) : g === "model" ? ["us.anthropic.claude-haiku-5-5", "us.openai.gpt-6.1-sol"] : g === "day" ? ["2026-10-10"] : g === "project" ? ["prj_1"] : "line";
    return send(res, 200, { group_by: g, from: "2026-09-11", to: "2026-10-10", ...page(usageRows(keys)) });
  }
  if (p[1] === "admin" && (req.headers.authorization ?? "").includes("_01JA0000000000000000000AAA.")) {
    // The developer key: the W4 reads are tenant_admin / reviewer / auditor only (x-noema-roles).
    return fail(res, 403, "permission_error", "permission_denied", "Permission denied.");
  }
  if (m === "GET" && url.pathname === "/v1/admin/calibrators") return send(res, 200, sdkFixture("listCalibrators.200").body);
  if (m === "GET" && url.pathname === "/v1/admin/labeling/runs") return send(res, 200, sdkFixture("listLabelingRuns.200").body);
  if (m === "GET" && url.pathname === "/v1/admin/labeling/dropped") return send(res, 200, sdkFixture("listLabelingDropped.200").body);
  return notFound(res);
}

const port = Number(process.argv[2] ?? "5181");
createServer((req, res) => {
  route(req, res).catch(() => res.destroy());
}).listen(port, "127.0.0.1", () => console.log(`fixture-api: http://127.0.0.1:${port}`));

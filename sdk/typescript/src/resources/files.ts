import { AghooConnectionError } from "../core/errors.js";
import type { Transport } from "../core/transport.js";
import type { DerivativeKind, FileObject, RequestOptions } from "../sdk.js";
import { type Json, seg } from "./shared.js";

/** The server closes `watchFile` after the first snapshot with one of these statuses. */
const TERMINAL_FILE_STATUSES: ReadonlySet<string> = new Set(["ready", "partial", "rejected", "failed", "deleted"]);

/** Uploads (validated and extracted asynchronously), status, derivatives. */
export class Files {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  /**
   * Multipart upload; the answer is 202 with the file in `quarantined` or `processing`. The fields go before the file
   * part (the server streams the body and authorizes `session_id` before it reads the file).
   */
  upload(
    file: Blob,
    params: { filename: string; purpose: "input"; session_id?: string },
    opts: RequestOptions = {},
  ): Promise<FileObject> {
    const form = new FormData();
    form.append("purpose", params.purpose);
    if (params.session_id !== undefined) form.append("session_id", params.session_id);
    form.append("file", file, params.filename);
    return this.#t.json({ method: "POST", path: "/v1/files", form, idempotent: true }, opts);
  }

  retrieve(fileId: string, opts: RequestOptions = {}): Promise<FileObject> {
    return this.#t.json({ method: "GET", path: `/v1/files/${seg(fileId)}` }, opts);
  }

  /**
   * Server-sent `file.updated` snapshots, starting with the current one, until ready / partial / rejected / failed /
   * deleted. The request starts when iteration starts; a stream that ends earlier throws AghooConnectionError.
   */
  watch(fileId: string, opts: RequestOptions = {}): AsyncIterable<FileObject> {
    const t = this.#t;
    const path = `/v1/files/${seg(fileId)}/events`;
    return {
      async *[Symbol.asyncIterator]() {
        const messages = await t.sse({ method: "GET", path }, opts);
        try {
          for await (const msg of messages) {
            if (msg.event !== "file.updated") continue;
            let snapshot: FileObject;
            try {
              snapshot = JSON.parse(msg.data) as FileObject;
            } catch (e) {
              throw new AghooConnectionError("malformed SSE event data", { cause: e });
            }
            yield snapshot;
            if (TERMINAL_FILE_STATUSES.has(snapshot.status)) return;
          }
        } finally {
          await messages.return(undefined);
        }
        throw new AghooConnectionError("file event stream ended before a terminal status");
      },
    };
  }

  delete(fileId: string, opts: RequestOptions = {}): Promise<Json<"deleteFile", 200>> {
    return this.#t.json({ method: "DELETE", path: `/v1/files/${seg(fileId)}` }, opts);
  }

  /** Derivative bytes; `page` (1-based) is required for page_images. Blob.type carries the content type. */
  derivative(
    fileId: string,
    kind: DerivativeKind,
    params: { page?: number } = {},
    opts: RequestOptions = {},
  ): Promise<Blob> {
    const path = `/v1/files/${seg(fileId)}/derivatives/${seg(kind)}`;
    return this.#t.blob({ method: "GET", path, query: { page: params.page }, accept: "*/*" }, opts);
  }
}

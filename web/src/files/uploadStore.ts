import type { AghooClient, Degradation, DerivativeKind, FileObject } from "@aghoo/sdk";
import { AghooError } from "@aghoo/sdk";
import { errorText } from "../lib/format";
import { Store } from "../lib/store";

export interface UploadItem {
  localId: string;
  filename: string;
  bytes: number;
  /** uploading: POST /v1/files in flight; watching: following file.updated; done: terminal; error: request failed. */
  phase: "uploading" | "watching" | "done" | "error";
  file: FileObject | null;
  /** A typed upload error (e.g. 413 file_too_large, 415 unsupported_media_type). */
  error: { code: string | null; message: string } | null;
}

/** What a derivative preview may show (spec-09 §6.8): PNG/JPEG blob URLs, plain or Markdown text, a PDF download. */
export type Preview =
  | { kind: "image"; url: string; type: string }
  | { kind: "markdown"; text: string }
  | { kind: "text"; text: string }
  | { kind: "download"; url: string; type: string; filename: string }
  | { kind: "unsupported"; type: string }
  | { kind: "error"; message: string };

export interface UploadState {
  items: UploadItem[];
  /** Degradations seen on turns, by file id (the "active content removed" notice comes from these). */
  notices: Record<string, Degradation[]>;
  previews: Record<string, Preview>;
}

/** Content types rendered as images. Never SVG (spec-09 §8: SVG is shown only as its raster derivative). */
export const IMAGE_TYPES: ReadonlySet<string> = new Set(["image/png", "image/jpeg"]);
const TERMINAL: ReadonlySet<string> = new Set(["ready", "rejected", "failed", "deleted"]);
const POLL_MS = 2000;
const POLL_MAX = 150;

export function previewKey(fileId: string, kind: DerivativeKind, page?: number): string {
  return page === undefined ? `${fileId}/${kind}` : `${fileId}/${kind}/${page}`;
}

/** The file needs no more updates: a terminal status, or partial with nothing pending. */
export function isSettled(f: FileObject): boolean {
  return TERMINAL.has(f.status) || (f.status === "partial" && !f.derivatives.some((d) => d.status === "pending"));
}

/** Maps a derivative blob to a preview by its content type (an allowlist; anything else is not rendered). */
export async function toPreview(blob: Blob, filename: string, createUrl: (b: Blob) => string): Promise<Preview> {
  const type = blob.type.split(";")[0]?.trim().toLowerCase() ?? "";
  if (IMAGE_TYPES.has(type)) return { kind: "image", url: createUrl(blob), type };
  if (type === "text/markdown") return { kind: "markdown", text: await blob.text() };
  if (type === "text/plain") return { kind: "text", text: await blob.text() };
  if (type === "application/pdf") return { kind: "download", url: createUrl(blob), type, filename };
  return { kind: "unsupported", type: type || "unknown" };
}

export interface UploadDeps {
  createUrl?: (b: Blob) => string;
  revokeUrl?: (url: string) => void;
  sleep?: (ms: number) => Promise<void>;
}

/** W2 state: uploads, live status over the file events SSE, derivative previews and turn notices. */
export class UploadStore extends Store<UploadState> {
  readonly #client: AghooClient;
  readonly #createUrl: (b: Blob) => string;
  readonly #revokeUrl: (url: string) => void;
  readonly #sleep: (ms: number) => Promise<void>;
  #seq = 0;

  constructor(client: AghooClient, deps: UploadDeps = {}) {
    super({ items: [], notices: {}, previews: {} });
    this.#client = client;
    this.#createUrl = deps.createUrl ?? ((b) => URL.createObjectURL(b));
    this.#revokeUrl = deps.revokeUrl ?? ((u) => URL.revokeObjectURL(u));
    this.#sleep = deps.sleep ?? ((t) => new Promise((r) => setTimeout(r, t)));
  }

  #patch(localId: string, patch: Partial<UploadItem>): void {
    this.set((s) => ({ ...s, items: s.items.map((i) => (i.localId === localId ? { ...i, ...patch } : i)) }));
  }

  /**
   * Starts one upload (the item appears at once under `localId`) and follows its status until it settles; `done`
   * resolves with the last snapshot (null when the upload request failed).
   */
  upload(file: Blob, filename: string, sessionId?: string): { localId: string; done: Promise<FileObject | null> } {
    this.#seq += 1;
    const localId = `u${this.#seq}`;
    const item: UploadItem = { localId, filename, bytes: file.size, phase: "uploading", file: null, error: null };
    this.set((s) => ({ ...s, items: [item, ...s.items] }));
    return { localId, done: this.#run(localId, file, filename, sessionId) };
  }

  item(localId: string): UploadItem | undefined {
    return this.state.items.find((i) => i.localId === localId);
  }

  async #run(localId: string, file: Blob, filename: string, sessionId: string | undefined): Promise<FileObject | null> {
    let current: FileObject;
    try {
      const params = sessionId === undefined ? { filename, purpose: "input" as const } : { filename, purpose: "input" as const, session_id: sessionId };
      current = await this.#client.files.upload(file, params);
    } catch (e) {
      this.#patch(localId, { phase: "error", error: { code: e instanceof AghooError ? String(e.code) : null, message: errorText(e) } });
      return null;
    }
    this.#patch(localId, { phase: isSettled(current) ? "done" : "watching", file: current });
    try {
      if (!isSettled(current)) {
        for await (const snap of this.#client.files.watch(current.id)) {
          current = snap;
          this.#patch(localId, { file: snap });
        }
      }
      // The SDK ends the watch at `partial` even while OCR is pending; follow the rest by polling (bounded).
      for (let i = 0; i < POLL_MAX && !isSettled(current); i++) {
        await this.#sleep(POLL_MS);
        current = await this.#client.files.retrieve(current.id);
        this.#patch(localId, { file: current });
      }
      this.#patch(localId, { phase: "done" });
    } catch (e) {
      this.#patch(localId, { phase: "error", error: { code: e instanceof AghooError ? String(e.code) : null, message: errorText(e) } });
    }
    return current;
  }

  /** Loads one derivative preview (page_images need a 1-based page). */
  async preview(fileId: string, kind: DerivativeKind, page?: number): Promise<void> {
    const key = previewKey(fileId, kind, page);
    const old = this.state.previews[key];
    if (old !== undefined && old.kind !== "error") return;
    let p: Preview;
    try {
      const blob = await this.#client.files.derivative(fileId, kind, page === undefined ? {} : { page });
      p = await toPreview(blob, `${fileId}-${kind}${page === undefined ? "" : `-${page}`}`, this.#createUrl);
    } catch (e) {
      p = { kind: "error", message: errorText(e) };
    }
    this.set((s) => ({ ...s, previews: { ...s.previews, [key]: p } }));
  }

  /** Records turn degradations that name a file (W2 shows "active content removed" from these). */
  noteDegradations(list: Degradation[]): void {
    const withFile = list.filter((d) => d.file_id !== undefined);
    if (withFile.length === 0) return;
    this.set((s) => {
      const notices = { ...s.notices };
      for (const d of withFile) {
        const id = d.file_id as string;
        const prev = notices[id] ?? [];
        if (!prev.some((x) => x.reason === d.reason && x.to === d.to && x.model === d.model)) notices[id] = [...prev, d];
      }
      return { ...s, notices };
    });
  }

  async remove(fileId: string): Promise<void> {
    await this.#client.files.delete(fileId);
    for (const [key, p] of Object.entries(this.state.previews)) {
      if (key.startsWith(`${fileId}/`) && (p.kind === "image" || p.kind === "download")) this.#revokeUrl(p.url);
    }
    this.set((s) => ({ ...s, items: s.items.filter((i) => i.file?.id !== fileId) }));
  }
}

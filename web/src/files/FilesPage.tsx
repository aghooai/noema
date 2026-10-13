import { type DragEvent, type ReactNode, useRef, useState } from "react";
import { useStore } from "../lib/store";
import { FileCard } from "./FileCard";
import type { UploadStore } from "./uploadStore";

/** W2: drop or pick files, follow their status live, preview derivatives. */
export function FilesPage({ uploads, sessionId }: { uploads: UploadStore; sessionId: string | null }): ReactNode {
  const s = useStore(uploads);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const start = (files: FileList | null): void => {
    for (const f of files ?? []) uploads.upload(f, f.name, sessionId ?? undefined);
  };
  const drop = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    setOver(false);
    start(e.dataTransfer.files);
  };
  return (
    <div className="files-page">
      <h2>Uploads</h2>
      <div
        className={over ? "dropzone over" : "dropzone"}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
        data-testid="dropzone"
      >
        Drop files here, or{" "}
        <button type="button" className="link-button" onClick={() => input.current?.click()}>
          choose files
        </button>
        <input ref={input} type="file" multiple hidden onChange={(e) => { start(e.target.files); e.target.value = ""; }} data-testid="file-input" />
        <div className="muted">
          {sessionId === null ? "Uploads are not attached to a session." : `Uploads are attached to session ${sessionId}.`} Files are checked and converted in a sandbox; models only ever see the derivatives.
        </div>
      </div>
      {s.items.length === 0 && <div className="muted">No uploads yet.</div>}
      {s.items.map((i) => (
        <FileCard
          key={i.localId}
          item={i}
          notices={i.file === null ? [] : (s.notices[i.file.id] ?? [])}
          previews={s.previews}
          onPreview={(id, kind, page) => void uploads.preview(id, kind, page)}
        />
      ))}
    </div>
  );
}

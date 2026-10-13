import type { Degradation, FileObject, FileRejectionCode } from "@aghoo/sdk";
import type { ReactNode } from "react";
import { degradationText } from "../inspector/Inspector";
import { SafeMarkdown } from "../markdown/SafeMarkdown";
import { type Preview, previewKey, type UploadItem } from "./uploadStore";

/** Typed rejection messages (FileObject.rejection.code, spec-07 §2). */
export const REJECTION_TEXT: Record<FileRejectionCode, string> = {
  type_mismatch: "The file's content does not match its type.",
  not_allowlisted: "This file type is not accepted.",
  file_too_large: "The file is too large.",
  too_many_pages: "The document has too many pages.",
  duration_exceeded: "The recording is too long.",
  pixel_limit: "The image has too many pixels.",
  encrypted_pdf: "Encrypted PDFs are not accepted.",
  decompression_bomb: "The file expands to an unsafe size.",
  corrupt: "The file is corrupt or truncated.",
  unsupported_codec: "The media codec is not supported.",
  empty_content: "The file has no content.",
  malware_detected: "The file was flagged as malware.",
};

/** Typed upload errors (HTTP responses of POST /v1/files). */
export const UPLOAD_ERROR_TEXT: Record<string, string> = {
  file_too_large: "The file is larger than the upload limit for its type.",
  unsupported_media_type: "This file type is not supported (for example DOCX, SVG, audio or video).",
  request_too_large: "The request is too large.",
  session_not_found: "The session does not exist.",
  rpm_exceeded: "Too many requests; try again shortly.",
};

function Status({ file }: { file: FileObject }): ReactNode {
  return <span className={`file-status status-${file.status}`} data-testid="file-status">{file.status}</span>;
}

export function ActiveContentNotice({ notices }: { notices: Degradation[] }): ReactNode {
  const removed = notices.filter((d) => d.reason === "active_content_removed");
  if (removed.length === 0) return null;
  return (
    <div className="notice notice-active-content" data-testid="active-content-notice" role="status">
      Active content removed: scripts, actions or embedded files were stripped from this file before any model saw it.
      {removed.map((d, i) => (
        <div key={i} className="muted">{degradationText(d)}</div>
      ))}
    </div>
  );
}

export function PreviewView({ preview, label }: { preview: Preview; label: string }): ReactNode {
  switch (preview.kind) {
    case "image":
      return <img className="preview-image" src={preview.url} alt={label} data-testid="preview-image" />;
    case "markdown":
      return <div className="preview-text" data-testid="preview-markdown"><SafeMarkdown text={preview.text} /></div>;
    case "text":
      return <pre className="preview-text" data-testid="preview-text">{preview.text}</pre>;
    case "download":
      return (
        <a href={preview.url} download={preview.filename} data-testid="preview-download">
          Download {label} (not displayed in the browser)
        </a>
      );
    case "unsupported":
      return <div className="muted">No preview for {preview.type}.</div>;
    case "error":
      return <div className="error">{preview.message}</div>;
  }
}

export interface FileCardProps {
  item: UploadItem;
  notices: Degradation[];
  compact?: boolean;
  previews?: Record<string, Preview>;
  onPreview?: (fileId: string, kind: FileObject["derivatives"][number]["kind"], page?: number) => void;
}

/** One upload: live status, typed rejection or failure, derivatives with previews, the active-content notice. */
export function FileCard({ item, notices, compact = false, previews = {}, onPreview }: FileCardProps): ReactNode {
  const f = item.file;
  return (
    <div className={compact ? "file-card compact" : "file-card"} data-testid="file-card">
      <div className="file-head">
        <strong>{item.filename}</strong> {f === null ? <span className="file-status">{item.phase === "error" ? "failed to upload" : "uploading…"}</span> : <Status file={f} />}
        {f !== null && !compact && <span className="muted"> {f.media_type} · {f.bytes.toLocaleString("en-US")} bytes · {f.id}</span>}
      </div>
      {item.error !== null && (
        <div className="error" data-testid="upload-error">
          {(item.error.code !== null ? UPLOAD_ERROR_TEXT[item.error.code] : undefined) ?? item.error.message}
          {item.error.code !== null && <code> ({item.error.code})</code>}
        </div>
      )}
      {f?.rejection !== undefined && (
        <div className="error" data-testid="file-rejection">
          Rejected: {REJECTION_TEXT[f.rejection.code] ?? f.rejection.message} <code>({f.rejection.code})</code>
        </div>
      )}
      {f?.failure !== undefined && (
        <div className="error" data-testid="file-failure">
          Extraction failed: {f.failure.message} <code>({f.failure.code}, retryable)</code>
        </div>
      )}
      <ActiveContentNotice notices={notices} />
      {!compact && f !== null && f.derivatives.length > 0 && (
        <ul className="derivatives" data-testid="derivatives">
          {f.derivatives.map((d) => {
            const pages = d.kind === "page_images" ? Array.from({ length: Math.min(d.pages ?? 1, 8) }, (_, i) => i + 1) : [undefined];
            return (
              <li key={d.id} data-testid={`derivative-${d.kind}`}>
                {d.kind} <span className={`derivative-status status-${d.status}`}>{d.status}</span>
                {d.status === "ready" && onPreview !== undefined && pages.map((page) => (
                  <button key={page ?? 0} type="button" className="link-button" onClick={() => onPreview(f.id, d.kind, page)}>
                    {page === undefined ? "preview" : `page ${page}`}
                  </button>
                ))}
                {pages.map((page) => {
                  const p = previews[previewKey(f.id, d.kind, page)];
                  return p === undefined ? null : <PreviewView key={page ?? 0} preview={p} label={`${d.kind}${page === undefined ? "" : ` page ${page}`}`} />;
                })}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

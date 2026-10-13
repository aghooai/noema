import { type ReactNode, useEffect, useRef, useState } from "react";
import { FileCard } from "../files/FileCard";
import type { UploadStore } from "../files/uploadStore";
import { Inspector } from "../inspector/Inspector";
import { unixTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { Attachment, ChatStore } from "./chatStore";
import { Conversation } from "./Conversation";

function SessionsPane({ chat }: { chat: ChatStore }): ReactNode {
  const s = useStore(chat);
  return (
    <nav className="pane pane-sessions" aria-label="Sessions" data-testid="sessions">
      <h2>Sessions</h2>
      <button type="button" onClick={() => void chat.newSession()} data-testid="new-session">
        + New session
      </button>
      {s.sessionsError !== null && <div className="error">{s.sessionsError}</div>}
      <ul>
        {s.sessions.map((x) => (
          <li key={x.id}>
            <button type="button" className={x.id === s.sessionId ? "session active" : "session"} onClick={() => void chat.selectSession(x.id)} title={x.id}>
              {x.metadata.title ?? x.id} <small>{unixTime(x.created_at)}</small>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Composer({ chat, uploads }: { chat: ChatStore; uploads: UploadStore }): ReactNode {
  const s = useStore(chat);
  const u = useStore(uploads);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const items = pending.map((id) => u.items.find((i) => i.localId === id)).filter((i) => i !== undefined);
  const usable = items.filter((i) => i.file !== null && i.file.status !== "rejected" && i.file.status !== "failed" && i.file.status !== "deleted");
  const attach = (files: FileList | null): void => {
    if (files === null || s.sessionId === null) return;
    const ids = [...files].map((f) => uploads.upload(f, f.name, s.sessionId ?? undefined).localId);
    setPending((p) => [...p, ...ids]);
  };
  const send = (): void => {
    const atts: Attachment[] = usable.map((i) => ({ fileId: i.file!.id, filename: i.filename, mediaType: i.file!.media_type }));
    void chat.send(text, atts);
    setText("");
    setPending([]);
  };
  return (
    <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }} data-testid="composer">
      {items.length > 0 && (
        <div className="composer-attachments">
          {items.map((i) => (
            <FileCard key={i.localId} item={i} notices={i.file === null ? [] : (u.notices[i.file.id] ?? [])} compact />
          ))}
        </div>
      )}
      <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Type a message…" aria-label="Message" disabled={s.sessionId === null} rows={3} data-testid="message-input" />
      <input ref={fileInput} type="file" hidden onChange={(e) => { attach(e.target.files); e.target.value = ""; }} data-testid="attach-input" />
      <button type="button" onClick={() => fileInput.current?.click()} disabled={s.sessionId === null} title="Attach a file">
        📎
      </button>
      {s.sending ? (
        <button type="button" onClick={() => chat.cancel()} data-testid="cancel">
          Stop
        </button>
      ) : (
        <button type="submit" disabled={s.sessionId === null || (text.trim() === "" && usable.length === 0)} data-testid="send">
          Send
        </button>
      )}
      {s.sendError !== null && <div className="error" data-testid="send-error">{s.sendError}</div>}
    </form>
  );
}

function InspectorPane({ chat }: { chat: ChatStore }): ReactNode {
  const s = useStore(chat);
  const view = chat.inspected();
  return (
    <aside className="pane pane-inspector" aria-label="Decision inspector" data-testid="inspector-pane">
      <h2>Decision inspector</h2>
      {s.inspectorError !== null && <div className="error">{s.inspectorError}</div>}
      {s.inspectorLoading && <div className="muted">loading…</div>}
      {view === null ? <div className="muted">Pick an answer to inspect it.</div> : <Inspector view={view} />}
    </aside>
  );
}

/** W1: sessions | conversation | decision inspector. */
export function ChatPage({ chat, uploads }: { chat: ChatStore; uploads: UploadStore }): ReactNode {
  const s = useStore(chat);
  useEffect(() => {
    void chat.loadSessions();
  }, [chat]);
  return (
    <div className="chat-layout">
      <SessionsPane chat={chat} />
      <main className="pane pane-conversation" aria-label="Conversation">
        <h2>Conversation</h2>
        {s.historyError !== null && <div className="error">{s.historyError}</div>}
        {s.sessionId === null ? <div className="muted">Pick or create a session.</div> : <Conversation messages={s.messages} live={s.live} views={chat.views} inspectedTurnId={s.inspectedTurnId} onInspect={(id) => void chat.inspect(id)} />}
        <Composer chat={chat} uploads={uploads} />
      </main>
      <InspectorPane chat={chat} />
    </div>
  );
}

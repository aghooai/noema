import type { ReactNode } from "react";
import { VerificationBadge } from "../inspector/Inspector";
import { SafeMarkdown } from "../markdown/SafeMarkdown";
import type { ChatMessage } from "./chatStore";
import type { TurnView } from "./turnState";

export interface ConversationProps {
  messages: ChatMessage[];
  live: TurnView | null;
  views: ReadonlyMap<string, TurnView>;
  inspectedTurnId: string | null;
  onInspect: (turnId: string) => void;
}

function AssistantBody({ text, view }: { text: string; view: TurnView | undefined }): ReactNode {
  return (
    <>
      <SafeMarkdown text={text} />
      {view?.verification != null && <VerificationBadge verification={view.verification} />}
    </>
  );
}

/** The streamed answer: committed text only, "Refining…" while an invisible restart is in progress. */
export function LiveTurn({ view }: { view: TurnView }): ReactNode {
  return (
    <div className="message message-assistant message-live" data-testid="live-turn" aria-live="polite">
      <div className="message-role">Assistant</div>
      {view.refining && (
        <div className="refining" data-testid="refining">
          Refining…
        </div>
      )}
      {view.text === "" && !view.refining ? <div className="typing">…</div> : <SafeMarkdown text={view.text} />}
      {view.errors.map((e, i) => (
        <div key={i} className="error" data-testid="turn-error">
          {e.code}: {e.message}
        </div>
      ))}
    </div>
  );
}

export function Conversation(p: ConversationProps): ReactNode {
  return (
    <div className="conversation" data-testid="conversation">
      {p.messages.map((m) => (
        <div key={m.key} className={`message message-${m.role}${m.turnId !== null && m.turnId === p.inspectedTurnId && m.role === "assistant" ? " inspected" : ""}`} data-testid={`message-${m.role}`}>
          <div className="message-role">
            {m.role === "user" ? "You" : "Assistant"}
            {m.role === "assistant" && m.turnId !== null && (
              <button type="button" className="link-button" onClick={() => p.onInspect(m.turnId as string)} data-testid="inspect-button">
                inspect
              </button>
            )}
          </div>
          {m.role === "user" ? <div className="message-text">{m.text}</div> : <AssistantBody text={m.text} view={m.turnId === null ? undefined : p.views.get(m.turnId)} />}
          {m.attachments.length > 0 && (
            <ul className="attachments">
              {m.attachments.map((a) => (
                <li key={a.fileId}>📎 {a.filename}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {p.live !== null && <LiveTurn view={p.live} />}
    </div>
  );
}

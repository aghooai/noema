import type { AghooClient, Degradation, InputPart, Session, TurnCreateParams } from "@aghoo/sdk";
import { errorText } from "../lib/format";
import { Store, take } from "../lib/store";
import { applyEvent, emptyTurn, fromTurnObject, type TurnView } from "./turnState";

export interface Attachment {
  fileId: string;
  filename: string;
  mediaType: string;
}

export interface ChatMessage {
  key: string;
  role: "user" | "assistant";
  text: string;
  turnId: string | null;
  attachments: Attachment[];
}

export interface ChatState {
  sessions: Session[];
  sessionsError: string | null;
  sessionId: string | null;
  messages: ChatMessage[];
  historyError: string | null;
  /** The turn being streamed (shown below the history until turn.completed). */
  live: TurnView | null;
  sending: boolean;
  sendError: string | null;
  /** The turn shown in the inspector. */
  inspectedTurnId: string | null;
  inspectorLoading: boolean;
  inspectorError: string | null;
}

const SESSIONS_SHOWN = 50;
const HISTORY_SHOWN = 500;

export function partFor(a: Attachment): InputPart {
  return a.mediaType.startsWith("image/") ? { type: "input_image", file_id: a.fileId } : { type: "input_file", file_id: a.fileId };
}

/** W1 state: sessions, the selected session's history, the live turn and the inspector selection. */
export class ChatStore extends Store<ChatState> {
  readonly #client: AghooClient;
  readonly #onDegradations: (d: Degradation[]) => void;
  /** Turn views by turn id: live turns as streamed, stored turns once fetched for the inspector. */
  readonly views = new Map<string, TurnView>();
  #abort: (() => void) | null = null;
  #seq = 0;

  constructor(client: AghooClient, onDegradations: (d: Degradation[]) => void = () => undefined) {
    super({
      sessions: [],
      sessionsError: null,
      sessionId: null,
      messages: [],
      historyError: null,
      live: null,
      sending: false,
      sendError: null,
      inspectedTurnId: null,
      inspectorLoading: false,
      inspectorError: null,
    });
    this.#client = client;
    this.#onDegradations = onDegradations;
  }

  #key(): string {
    this.#seq += 1;
    return `m${this.#seq}`;
  }

  async loadSessions(): Promise<void> {
    try {
      const sessions = await take(this.#client.sessions.list({ limit: SESSIONS_SHOWN }), SESSIONS_SHOWN);
      this.set({ sessions, sessionsError: null });
    } catch (e) {
      this.set({ sessionsError: errorText(e) });
    }
  }

  async newSession(): Promise<void> {
    try {
      const s = await this.#client.sessions.create({ metadata: { channel: "web" } });
      this.set((st) => ({ ...st, sessions: [s, ...st.sessions], sessionsError: null }));
      await this.selectSession(s.id);
    } catch (e) {
      this.set({ sessionsError: errorText(e) });
    }
  }

  async selectSession(id: string): Promise<void> {
    this.set({ sessionId: id, messages: [], historyError: null, live: null, inspectedTurnId: null, inspectorError: null });
    try {
      const turns = await take(this.#client.sessions.turns(id, { limit: 100 }), HISTORY_SHOWN);
      if (this.state.sessionId !== id) return;
      const messages = turns.map((t) => ({ key: this.#key(), role: t.role, text: t.text, turnId: t.turn_id, attachments: [] }));
      this.set({ messages });
    } catch (e) {
      if (this.state.sessionId === id) this.set({ historyError: errorText(e) });
    }
  }

  /** Streams one turn in the selected session; the inspector follows it. */
  async send(text: string, attachments: Attachment[] = []): Promise<void> {
    const sessionId = this.state.sessionId;
    if (sessionId === null || this.state.sending || (text.trim() === "" && attachments.length === 0)) return;
    const content: InputPart[] = [];
    if (text.trim() !== "") content.push({ type: "input_text", text });
    for (const a of attachments) content.push(partFor(a));
    const params: TurnCreateParams = { session_id: sessionId, input: [{ role: "user", content }] };
    if (attachments.length > 0) params.wait_for_files = true;
    const user: ChatMessage = { key: this.#key(), role: "user", text, turnId: null, attachments };
    let view = emptyTurn();
    this.set((s) => ({ ...s, messages: [...s.messages, user], live: view, sending: true, sendError: null }));
    const stream = this.#client.turns.stream(params);
    this.#abort = () => stream.abort();
    try {
      for await (const ev of stream) {
        view = applyEvent(view, ev);
        if (view.turnId !== null) {
          this.views.set(view.turnId, view);
          if (ev.type === "turn.created") this.set({ inspectedTurnId: view.turnId });
        }
        if (ev.type === "preflight.completed" || ev.type === "route.selected" || ev.type === "generation.started") {
          this.#onDegradations(ev.degradations);
        }
        if (this.state.sessionId === sessionId) this.set({ live: view });
      }
    } catch (e) {
      this.set({ sendError: errorText(e) });
    } finally {
      this.#abort = null;
      if (this.state.sessionId === sessionId) {
        const done = view.turnId === null && view.text === "" ? [] : [{ key: this.#key(), role: "assistant" as const, text: view.text, turnId: view.turnId, attachments: [] }];
        this.set((s) => ({
          ...s,
          messages: [...s.messages.map((m) => (m === user ? { ...m, turnId: view.turnId } : m)), ...done],
          live: null,
          sending: false,
        }));
      } else {
        this.set({ sending: false });
      }
    }
  }

  /** Explicit, final cancel of the streaming turn (POST /v1/turns/{id}/cancel through the SDK). */
  cancel(): void {
    this.#abort?.();
  }

  async inspect(turnId: string): Promise<void> {
    this.set({ inspectedTurnId: turnId, inspectorError: null });
    if (this.views.has(turnId)) return;
    this.set({ inspectorLoading: true });
    try {
      const view = fromTurnObject(await this.#client.turns.retrieve(turnId));
      this.views.set(turnId, view);
      this.#onDegradations(view.degradations);
      this.set({ inspectorLoading: false });
    } catch (e) {
      if (this.state.inspectedTurnId === turnId) this.set({ inspectorLoading: false, inspectorError: errorText(e) });
    }
  }

  inspected(): TurnView | null {
    const id = this.state.inspectedTurnId;
    return id === null ? null : (this.views.get(id) ?? null);
  }
}

import { type ReactNode, useEffect, useMemo, useState } from "react";
import type { Api } from "./api/client";
import type { AuthState } from "./api/protocol";
import { CalibrationPage } from "./calibration/CalibrationPage";
import { CalibrationStore } from "./calibration/calibrationStore";
import { ChatPage } from "./chat/ChatPage";
import { ChatStore } from "./chat/chatStore";
import { FilesPage } from "./files/FilesPage";
import { UploadStore } from "./files/uploadStore";
import { errorText } from "./lib/format";
import { useStore } from "./lib/store";
import { UsagePage } from "./usage/UsagePage";
import { UsageStore } from "./usage/usageStore";

export const ROUTES = [
  { hash: "#/chat", label: "Chat" },
  { hash: "#/files", label: "Uploads" },
  { hash: "#/calibration", label: "Calibration" },
  { hash: "#/usage", label: "Usage" },
] as const;

export interface Stores {
  chat: ChatStore;
  uploads: UploadStore;
  calibration: CalibrationStore;
  usage: UsageStore;
}

export function createStores(api: Api): Stores {
  const uploads = new UploadStore(api.client);
  return {
    uploads,
    chat: new ChatStore(api.client, (d) => uploads.noteDegradations(d)),
    calibration: new CalibrationStore(api.client),
    usage: new UsageStore(api.client),
  };
}

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/chat");
  useEffect(() => {
    const on = (): void => setHash(window.location.hash || "#/chat");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

/** Dev login (spec-09 §2): pick a server-side key alias; the key itself stays in the proxy. */
export function LoginPage({ auth, onLogin, error }: { auth: AuthState; onLogin: (alias: string) => void; error: string | null }): ReactNode {
  return (
    <main className="login" data-testid="login">
      <h1>Noema (dev login)</h1>
      <p>Choose the API key this browser session uses. Keys stay on the dev proxy; the browser only holds an HttpOnly session cookie.</p>
      {auth.aliases.length === 0 && <p className="error">The proxy has no API keys configured (NOEMA_UI_API_KEYS).</p>}
      <ul>
        {auth.aliases.map((a) => (
          <li key={a}>
            <button type="button" onClick={() => onLogin(a)} data-testid={`login-${a}`}>
              Log in as {a}
            </button>
          </li>
        ))}
      </ul>
      {error !== null && <p className="error">{error}</p>}
    </main>
  );
}

function Shell({ stores, auth, onLogout }: { stores: Stores; auth: AuthState; onLogout: () => void }): ReactNode {
  const hash = useHash();
  const chat = useStore(stores.chat);
  let page: ReactNode;
  if (hash === "#/files") page = <FilesPage uploads={stores.uploads} sessionId={chat.sessionId} />;
  else if (hash === "#/calibration") page = <CalibrationPage store={stores.calibration} />;
  else if (hash === "#/usage") page = <UsagePage store={stores.usage} />;
  else page = <ChatPage chat={stores.chat} uploads={stores.uploads} />;
  return (
    <div className="app">
      <header className="topbar">
        <strong>Noema</strong>
        <nav>
          {ROUTES.map((r) => (
            <a key={r.hash} href={r.hash} className={hash === r.hash || (r.hash === "#/chat" && !ROUTES.some((x) => x.hash === hash)) ? "active" : ""}>
              {r.label}
            </a>
          ))}
        </nav>
        <span className="who" data-testid="who">
          {auth.alias}{" "}
          <button type="button" onClick={onLogout} data-testid="logout">
            Log out
          </button>
        </span>
      </header>
      {page}
    </div>
  );
}

/** The SPA root: the dev-login gate, then the shell. Stores are rebuilt per login, so no data outlives its alias. */
export function App({ api }: { api: Api }): ReactNode {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alias = auth?.authenticated === true ? auth.alias : null;
  const stores = useMemo(() => (alias === null ? null : createStores(api)), [api, alias]);
  useEffect(() => {
    api.auth.session().then(setAuth, (e: unknown) => setError(errorText(e)));
  }, [api]);
  const run = (p: Promise<AuthState>): void => {
    p.then((a) => { setAuth(a); setError(null); }, (e: unknown) => setError(errorText(e)));
  };
  if (auth === null) return <main className="login">{error ?? "connecting…"}</main>;
  if (!auth.authenticated || stores === null) return <LoginPage auth={auth} error={error} onLogin={(a) => run(api.auth.login(a))} />;
  return <Shell stores={stores} auth={auth} onLogout={() => run(api.auth.logout())} />;
}

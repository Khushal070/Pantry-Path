import { useCallback, useEffect, useState } from "react";
import { api, ApiError, whenLoggedOut } from "./api";
import type { AppConfig, Message, Preferences, SearchRequest, Session, SessionSummary, User } from "./types";
import { AuthScreen } from "./components/AuthScreen";
import { ChatView } from "./components/ChatView";
import { PreferencesDialog } from "./components/PreferencesDialog";
import { Sidebar } from "./components/Sidebar";
import { XIcon } from "./icons";

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [pendingText, setPendingText] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"preferences" | "onboarding" | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  const showError = useCallback((e: unknown) => setToast(errorText(e)), []);

  // Back to the login screen, with nothing of the last user left on screen
  const signedOut = useCallback((notice: string | null) => {
    setUser(null);
    setSessions([]);
    setSession(null);
    setDialog(null);
    setPendingText(null);
    setMenuOpen(false);
    setAuthNotice(notice);
  }, []);

  // A request that needed a login got 401: the login expired, or ended elsewhere
  useEffect(() => {
    whenLoggedOut(() => signedOut("Your login has ended. Please log in again."));
    return () => whenLoggedOut(null);
  }, [signedOut]);

  const load = useCallback(async () => {
    setLoadError(null);
    setBooting(true);
    try {
      setConfig(await api.config());
      try {
        setUser(await api.me());
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 401)) throw e;
        setUser(null); // not logged in: the login screen
      }
    } catch (e) {
      setLoadError(errorText(e));
    } finally {
      setBooting(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.sessions());
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) showError(e);
    }
  }, [showError]);

  // A new login (or a different user) starts on a blank chat with that user's chat list
  const userId = user?.id ?? null;
  useEffect(() => {
    setSession(null);
    setSessions([]);
    if (userId) refreshSessions();
  }, [userId, refreshSessions]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 6000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function onAuthed(next: User, isNew: boolean) {
    setAuthNotice(null);
    setUser(next);
    setDialog(isNew ? "onboarding" : null);
  }

  async function logout() {
    try {
      await api.logout();
    } catch {
      // the server may already have ended the login; the browser forgets it either way
    }
    signedOut(null);
  }

  async function savePreferences(prefs: Preferences) {
    setUser(await api.savePreferences(prefs));
    setDialog(null);
  }

  async function deleteAccount(password: string) {
    await api.deleteAccount(password);
    signedOut("Your account and its chats were deleted.");
  }

  async function openSession(id: string) {
    setMenuOpen(false);
    try {
      setSession(await api.session(id));
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) showError(e);
    }
  }

  async function deleteSession(id: string) {
    const target = sessions.find((s) => s.id === id);
    if (!window.confirm(`Delete the chat "${target?.title ?? "this chat"}"?`)) return;
    try {
      await api.deleteSession(id);
      if (session?.id === id) setSession(null);
      refreshSessions();
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) showError(e);
    }
  }

  async function send(text: string): Promise<boolean> {
    setPendingText(text);
    try {
      let current = session;
      if (!current) {
        current = await api.createSession(); // a chat is made on its first message
        setSession(current);
      }
      const reply = await api.say(current.id, text);
      setSession(reply.session);
      refreshSessions();
      return true;
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) showError(e);
      return false;
    } finally {
      setPendingText(null);
    }
  }

  async function confirm(message: Message, request: SearchRequest, servings: number | null) {
    if (!session) return;
    const reply = await api.search(session.id, message.id, request, servings); // errors show on the card
    setSession(reply.session);
    refreshSessions();
  }

  async function rerun(message: Message) {
    if (!session) return;
    const reply = await api.rerun(session.id, message.id);
    setSession(reply.session);
  }

  async function askStronger(message: Message) {
    if (!session) return;
    const reply = await api.rerunStronger(session.id, message.id); // errors show on the result
    setSession(reply.session);
  }

  if (loadError) {
    return (
      <div className="splash">
        <h1>Pantry Path</h1>
        <p className="form-error">{loadError}</p>
        <p className="muted">
          Start the backend: <code>cd backend && uvicorn app.main:app --reload</code>
        </p>
        <button className="btn primary" onClick={load}>
          Try again
        </button>
      </div>
    );
  }
  if (booting || !config) {
    return (
      <div className="splash">
        <p className="muted">Loading Pantry Path…</p>
      </div>
    );
  }
  if (!user) {
    return <AuthScreen config={config} notice={authNotice} onAuthed={onAuthed} />;
  }

  return (
    <div className={`app${menuOpen ? " menu-open" : ""}`}>
      <Sidebar
        config={config}
        user={user}
        sessions={sessions}
        activeSessionId={session?.id ?? null}
        onPreferences={() => setDialog("preferences")}
        onLogout={logout}
        onNewChat={() => {
          setSession(null);
          setMenuOpen(false);
        }}
        onOpenSession={openSession}
        onDeleteSession={deleteSession}
        onClose={() => setMenuOpen(false)}
      />
      <div className="scrim" onClick={() => setMenuOpen(false)} aria-hidden="true" />
      <main className="main">
        <ChatView
          config={config}
          user={user}
          session={session}
          pendingText={pendingText}
          onSend={send}
          onConfirm={confirm}
          onRerun={rerun}
          onAskStronger={askStronger}
          onEditPreferences={() => setDialog("preferences")}
          onOpenMenu={() => setMenuOpen(true)}
        />
      </main>
      {dialog && (
        <PreferencesDialog
          key={`${user.id}-${dialog}`}
          config={config}
          user={user}
          onboarding={dialog === "onboarding"}
          onSave={savePreferences}
          onChangePassword={(current, next) => api.changePassword(current, next)}
          onDeleteAccount={deleteAccount}
          onClose={() => setDialog(null)}
        />
      )}
      {toast && (
        <div className="toast" role="status">
          <span>{toast}</span>
          <button className="icon-btn" onClick={() => setToast(null)} aria-label="Dismiss">
            <XIcon size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

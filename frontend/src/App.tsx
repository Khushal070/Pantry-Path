import { useCallback, useEffect, useState } from "react";
import { api, ApiError, whenLoggedOut } from "./api";
import type {
  AppConfig,
  BuildAction,
  BuildData,
  Message,
  Preferences,
  SavedListSummary,
  SearchRequest,
  Session,
  SessionSummary,
  User,
} from "./types";
import { AuthScreen } from "./components/AuthScreen";
import { ChatView } from "./components/ChatView";
import { PreferencesDialog } from "./components/PreferencesDialog";
import { SavedListPage } from "./components/SavedListPage";
import { Sidebar } from "./components/Sidebar";
import { XIcon } from "./icons";
import {
  LISTS_CHANGED,
  clearBoot,
  clearDevice,
  flushQueue,
  isOffline,
  loadSummaries,
  readBoot,
  saveBoot,
  setDeviceUser,
} from "./offline";

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
  const [lists, setLists] = useState<SavedListSummary[]>([]);
  const [openList, setOpenList] = useState<string | null>(null); // a saved list instead of the chat
  const [offline, setOffline] = useState(false);

  const showError = useCallback((e: unknown) => setToast(errorText(e)), []);

  // Back to the login screen, with nothing of the last user left on screen
  const signedOut = useCallback((notice: string | null) => {
    setUser((last) => {
      if (last) clearDevice(last.id); // this device keeps nothing of a logged-out user
      return null;
    });
    clearBoot();
    setLists([]);
    setOpenList(null);
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
      const nextConfig = await api.config();
      setConfig(nextConfig);
      setOffline(false);
      try {
        const me = await api.me();
        setUser(me);
        saveBoot(nextConfig, me);
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 401)) throw e;
        setUser(null); // not logged in: the login screen
        clearBoot();
      }
    } catch (e) {
      // No signal: open with this device's copy, so saved lists still work
      const boot = readBoot<AppConfig, User>();
      if (isOffline(e) && boot?.user) {
        setConfig(boot.config);
        setUser(boot.user);
        setOffline(true);
        return;
      }
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

  const refreshLists = useCallback(async () => {
    if (!user) return;
    try {
      const r = await loadSummaries(user.id);
      setLists(r.lists);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) showError(e);
    }
  }, [user, showError]);

  // A new login (or a different user) starts on a blank chat with that user's chat list
  const userId = user?.id ?? null;
  useEffect(() => {
    setDeviceUser(userId);
    setSession(null);
    setSessions([]);
    setOpenList(null);
    if (userId && !offline) {
      refreshSessions();
      flushQueue(userId).catch(() => 0); // ticks made offline last time
    }
  }, [userId, refreshSessions]);

  useEffect(() => {
    refreshLists();
    const changed = () => refreshLists();
    window.addEventListener(LISTS_CHANGED, changed);
    window.addEventListener("online", changed);
    return () => {
      window.removeEventListener(LISTS_CHANGED, changed);
      window.removeEventListener("online", changed);
    };
  }, [refreshLists]);

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

  async function build(message: Message, body: BuildAction) {
    if (!session) return;
    const reply = await api.build(session.id, message.id, body); // errors show on the card
    setSession(reply.session);
    refreshSessions();
  }

  // A build's products arrive in the background: refresh the chat until they have
  const lastMessage = session?.messages[session.messages.length - 1];
  const waitingForProducts =
    lastMessage?.kind === "build" &&
    (() => {
      const b = lastMessage.data as unknown as BuildData;
      if (b.state !== "choosing") return false;
      if (b.products?.status === "loading") return true;
      return b.additions.some((a) => a.status !== "clash" && !b.choices?.[a.name]);
    })();
  useEffect(() => {
    if (!waitingForProducts || !session) return;
    const id = session.id;
    const timer = window.setTimeout(async () => {
      try {
        const fresh = await api.session(id);
        setSession((now) => (now?.id === id ? fresh : now));
      } catch {
        // a missed refresh is fine: the next one comes
      }
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [waitingForProducts, session]);

  async function addMore(message: Message, text: string) {
    if (!session) return;
    const reply = await api.addMore(session.id, message.id, text); // errors show on the card
    setSession(reply.session);
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
          setOpenList(null);
          setSession(null);
          setMenuOpen(false);
        }}
        onOpenSession={(id) => {
          setOpenList(null);
          openSession(id);
        }}
        onDeleteSession={deleteSession}
        lists={lists}
        activeListId={openList}
        onOpenList={(id) => {
          setOpenList(id);
          setMenuOpen(false);
        }}
        onClose={() => setMenuOpen(false)}
      />
      <div className="scrim" onClick={() => setMenuOpen(false)} aria-hidden="true" />
      <main className="main">
        {offline && (
          <p className="callout warn small offline-banner">
            You're offline: your saved lists work, and ticks are sent when the signal is back. Chat needs a
            connection.
          </p>
        )}
        {openList ? (
          <SavedListPage
            listId={openList}
            mode={config.mode}
            onBack={() => setOpenList(null)}
            onDeleted={() => setOpenList(null)}
            onOpenMenu={() => setMenuOpen(true)}
          />
        ) : (
        <ChatView
          config={config}
          user={user}
          session={session}
          pendingText={pendingText}
          onSend={send}
          onConfirm={confirm}
          onBuild={build}
          onAddMore={addMore}
          onRerun={rerun}
          onAskStronger={askStronger}
          onEditPreferences={() => setDialog("preferences")}
          onOpenMenu={() => setMenuOpen(true)}
        />
        )}
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

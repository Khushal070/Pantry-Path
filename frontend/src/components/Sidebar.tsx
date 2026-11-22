import type { AppConfig, Preferences, SessionSummary, User } from "../types";
import { joinWords, labelOf, relativeTime } from "../format";
import { LeafIcon, LogOutIcon, PlusIcon, SlidersIcon, TrashIcon, XIcon } from "../icons";

export function preferencesLine(config: AppConfig | null, prefs: Preferences): string {
  const parts: string[] = [];
  if (prefs.diet !== "none") parts.push(labelOf(config?.diets, prefs.diet));
  if (prefs.avoid.length)
    parts.push(`no ${joinWords(prefs.avoid.map((a) => labelOf(config?.allergens, a).toLowerCase()))}`);
  if (prefs.zip) parts.push(`near ${prefs.zip}`);
  return parts.length ? parts.join(" · ") : "No food preferences saved yet";
}

export function Sidebar({
  config,
  user,
  sessions,
  activeSessionId,
  onPreferences,
  onLogout,
  onNewChat,
  onOpenSession,
  onDeleteSession,
  onClose,
}: {
  config: AppConfig;
  user: User;
  sessions: SessionSummary[];
  activeSessionId: string | null;
  onPreferences: () => void;
  onLogout: () => void;
  onNewChat: () => void;
  onOpenSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <aside className="sidebar" aria-label="Your account and chats">
      <div className="brand">
        <span className="brand-mark">
          <LeafIcon size={20} />
        </span>
        <span className="brand-name">Pantry Path</span>
        <button className="icon-btn only-mobile" onClick={onClose} aria-label="Close menu">
          <XIcon />
        </button>
      </div>

      <section className="user-card" aria-label="Logged in as">
        <div className="profile-row">
          <span className="avatar" aria-hidden="true">
            {user.name.charAt(0).toUpperCase()}
          </span>
          <div className="profile-text">
            <strong>{user.name}</strong>
            <span className="muted small">@{user.username}</span>
          </div>
        </div>
        <p className="muted small">{preferencesLine(config, user)}</p>
        <div className="user-actions">
          <button className="btn ghost small" onClick={onPreferences}>
            <SlidersIcon size={14} /> Preferences
          </button>
          <button className="btn ghost small" onClick={onLogout}>
            <LogOutIcon size={14} /> Log out
          </button>
        </div>
      </section>

      <button className="btn primary block" onClick={onNewChat}>
        <PlusIcon size={16} /> New chat
      </button>

      <nav className="chat-list" aria-label="Chats">
        {sessions.length === 0 && <p className="muted small pad">Your chats will appear here.</p>}
        {sessions.map((s) => (
          <div key={s.id} className={`chat-item${s.id === activeSessionId ? " active" : ""}`}>
            <button className="chat-open" onClick={() => onOpenSession(s.id)}>
              <span className="chat-title">{s.title}</span>
              <span className="chat-meta">{relativeTime(s.updated_at)}</span>
            </button>
            <button
              className="icon-btn chat-delete"
              onClick={() => onDeleteSession(s.id)}
              aria-label={`Delete chat ${s.title}`}
              title="Delete chat"
            >
              <TrashIcon size={14} />
            </button>
          </div>
        ))}
      </nav>

      <p className="disclaimer">
        Pantry Path gives information, not medical or religious advice. Always check product labels
        before buying.
      </p>
    </aside>
  );
}

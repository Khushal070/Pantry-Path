import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { AppConfig, Message, SearchRequest, Session, User } from "../types";
import { cap } from "../format";
import { AlertIcon, LeafIcon, MenuIcon, SendIcon } from "../icons";
import { ConfirmCard } from "./ConfirmCard";
import { SearchCard } from "./SearchCard";
import { preferencesLine } from "./Sidebar";

const EXAMPLES = [
  "I feel like quiche tonight",
  "Pad thai for 2, no peanuts or shellfish",
  "Something cozy: chicken pot pie for 6",
  "Vegan pancakes near 10001",
];

export function ChatView({
  config,
  user,
  session,
  pendingText,
  onSend,
  onConfirm,
  onRerun,
  onAskStronger,
  onEditPreferences,
  onOpenMenu,
}: {
  config: AppConfig;
  user: User;
  session: Session | null;
  pendingText: string | null;
  onSend: (text: string) => Promise<boolean>;
  onConfirm: (message: Message, request: SearchRequest, servings: number | null) => Promise<void>;
  onRerun: (message: Message) => Promise<void>;
  onAskStronger: (message: Message) => Promise<void>;
  onEditPreferences: () => void;
  onOpenMenu: () => void;
}) {
  const [text, setText] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const messages = session?.messages ?? [];
  const last = messages[messages.length - 1];

  useEffect(() => {
    const box = scroller.current;
    if (box && (messages.length || pendingText !== null)) {
      box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    } else {
      box?.scrollTo({ top: 0 });
    }
  }, [messages.length, pendingText]);

  useEffect(() => {
    input.current?.focus();
  }, [session?.id]);

  async function send(value = text) {
    const message = value.trim();
    if (!message || pendingText !== null) return;
    setText("");
    const ok = await onSend(message);
    if (!ok) setText(message);
    input.current?.focus();
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  }

  const empty = messages.length === 0 && pendingText === null;

  return (
    <section className="chat" aria-label="Chat">
      <header className="chat-head">
        <button className="icon-btn only-mobile" onClick={onOpenMenu} aria-label="Open menu">
          <MenuIcon />
        </button>
        <h1 className="chat-title-main">{session?.title ?? "New chat"}</h1>
        {config.mode === "demo" ? (
          <span className="pill warn" title="Synthetic stores and products; a keyword reader stands in for the model">
            Demo data
          </span>
        ) : (
          <span className="pill ok" title="Live store data and the champion model">
            Live
          </span>
        )}
      </header>

      <div className="messages" ref={scroller}>
        <div className="messages-inner">
          {empty && (
            <div className="welcome">
              <span className="welcome-mark">
                <LeafIcon size={28} />
              </span>
              <h2>What do you feel like cooking?</h2>
              <p className="muted">
                Name a dish. I'll adapt it to your diet, check every product's label at nearby stores and
                give you a shopping list.
              </p>
              <p className="profile-hint">
                Shopping for <strong>{user.name}</strong>: {preferencesLine(config, user)}.{" "}
                <button className="link" onClick={onEditPreferences}>
                  Edit preferences
                </button>
              </p>
              <div className="examples">
                {EXAMPLES.map((e) => (
                  <button key={e} className="example" onClick={() => send(e)}>
                    {e}
                  </button>
                ))}
              </div>
              {config.mode === "demo" && (
                <p className="callout warn small">
                  <AlertIcon size={16} /> Demo mode: stores and products are synthetic and a simple keyword reader
                  stands in for the model. It knows {String(config.engine.dishes ?? 70)} dishes. Set
                  PANTRYPATH_MODE=live on the backend for the real thing.
                </p>
              )}
            </div>
          )}

          {messages.map((m) => (
            <MessageView
              key={m.id}
              message={m}
              config={config}
              actionable={m === last && m.kind === "confirm" && !m.data?.submitted && pendingText === null}
              onConfirm={(request, servings) => onConfirm(m, request, servings)}
              onRerun={() => onRerun(m)}
              onAskStronger={() => onAskStronger(m)}
            />
          ))}

          {pendingText !== null && (
            <>
              <div className="bubble-row user">
                <div className="bubble user">{pendingText}</div>
              </div>
              <div className="bubble-row assistant">
                <div className="bubble assistant typing" aria-live="polite">
                  <span className="dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <span className="muted small">
                    Reading your message{config.mode === "live" ? " (the model can take a few seconds)" : ""}…
                  </span>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          ref={input}
          value={text}
          rows={1}
          maxLength={2000}
          placeholder="Tell me what you'd like to cook…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          aria-label="Message"
        />
        <button
          type="submit"
          className="btn primary send"
          disabled={!text.trim() || pendingText !== null}
          aria-label="Send"
        >
          <SendIcon size={18} />
        </button>
      </form>
    </section>
  );
}

function MessageView({
  message,
  config,
  actionable,
  onConfirm,
  onRerun,
  onAskStronger,
}: {
  message: Message;
  config: AppConfig;
  actionable: boolean;
  onConfirm: (request: SearchRequest, servings: number | null) => Promise<void>;
  onRerun: () => Promise<void>;
  onAskStronger: () => Promise<void>;
}) {
  if (message.role === "user") {
    return (
      <div className="bubble-row user">
        <div className="bubble user">{message.text}</div>
      </div>
    );
  }
  let body;
  if (message.kind === "confirm") {
    body = <ConfirmCard message={message} config={config} actionable={actionable} onConfirm={onConfirm} />;
  } else if (message.kind === "search") {
    body = (
      <SearchCard
        message={message}
        mode={config.mode}
        stronger={config.stronger_model}
        onRerun={onRerun}
        onAskStronger={onAskStronger}
      />
    );
  } else if (message.kind === "error") {
    body = (
      <div className="bubble assistant error" role="alert">
        {message.text}
      </div>
    );
  } else {
    body = <div className="bubble assistant">{cap(message.text)}</div>;
  }
  return (
    <div className={`bubble-row assistant${message.kind === "text" || message.kind === "error" ? "" : " wide"}`}>
      <span className="assistant-mark" aria-hidden="true">
        <LeafIcon size={16} />
      </span>
      {body}
    </div>
  );
}

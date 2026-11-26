import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { AppConfig, User } from "../types";
import { AlertIcon, InfoIcon, LeafIcon } from "../icons";

type Mode = "login" | "register";
const USERNAME = /^[a-z0-9][a-z0-9._-]{2,31}$/; // the backend's rule (schemas.py)

/** Log in, or create an account (which logs in too). */
export function AuthScreen({
  config,
  notice,
  onAuthed,
}: {
  config: AppConfig;
  notice: string | null;
  onAuthed: (user: User, isNew: boolean) => void;
}) {
  const [mode, setMode] = useState<Mode>("login");
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNotice, setShowNotice] = useState(true); // "you were logged out" etc., until the next try

  const cleanUsername = username.trim().toLowerCase();
  const valid =
    mode === "login"
      ? cleanUsername !== "" && password !== ""
      : name.trim() !== "" && USERNAME.test(cleanUsername) && password.length >= 8;

  function switchTo(next: Mode) {
    setMode(next);
    setError(null);
    setShowNotice(false);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    setShowNotice(false);
    try {
      const user =
        mode === "login"
          ? await api.login(cleanUsername, password)
          : await api.register(name.trim(), cleanUsername, password);
      onAuthed(user, mode === "register");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth-panel">
        <div className="auth-brand">
          <span className="brand-mark">
            <LeafIcon size={20} />
          </span>
          <span className="brand-name">Pantry Path</span>
        </div>
        <div>
          <h1 className="auth-title">{mode === "login" ? "Welcome back" : "Create your account"}</h1>
          <p className="muted">
            {mode === "login"
              ? "Log in to see your chats and your saved food preferences."
              : "Save your allergies and diet once, and every shopping list follows them."}
          </p>
        </div>

        {notice && showNotice && (
          <p className="callout info small">
            <InfoIcon size={16} /> {notice}
          </p>
        )}

        <form className="auth-form" onSubmit={submit} noValidate>
          {mode === "register" && (
            <label className="field">
              <span>Your name</span>
              <input
                value={name}
                maxLength={60}
                autoComplete="name"
                placeholder="e.g. Kenil"
                autoFocus
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          )}
          <label className="field">
            <span>Username</span>
            <input
              value={username}
              maxLength={32}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              autoFocus={mode === "login"}
              aria-invalid={mode === "register" && username !== "" && !USERNAME.test(cleanUsername)}
              onChange={(e) => setUsername(e.target.value)}
            />
            {mode === "register" && (
              <small className="hint">3 to 32 letters, numbers, dots, dashes or underscores.</small>
            )}
          </label>
          <label className="field">
            <span>Password</span>
            <span className="password-input">
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                maxLength={128}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                type="button"
                className="link small"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </span>
            {mode === "register" && <small className="hint">At least 8 characters.</small>}
          </label>

          {error && (
            <p className="form-error row gap-sm" role="alert">
              <AlertIcon size={16} /> {error}
            </p>
          )}

          <button type="submit" className="btn primary block" disabled={!valid || busy}>
            {busy ? "One moment…" : mode === "login" ? "Log in" : "Create account"}
          </button>
        </form>

        <p className="auth-switch">
          {mode === "login" ? (
            <>
              New here?{" "}
              <button className="link" onClick={() => switchTo("register")}>
                Create an account
              </button>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <button className="link" onClick={() => switchTo("login")}>
                Log in
              </button>
            </>
          )}
        </p>
        {config.mode === "demo" && (
          <p className="muted small auth-foot">Demo mode: the stores and products are synthetic.</p>
        )}
      </div>
      <p className="disclaimer auth-disclaimer">
        Pantry Path gives information, not medical or religious advice. Always check product labels before
        buying.
      </p>
    </div>
  );
}

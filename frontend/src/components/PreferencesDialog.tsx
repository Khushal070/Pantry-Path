import { api } from "../api";
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { AppConfig, Diet, Preferences, User } from "../types";
import { ChipGroup, Segmented, toggle } from "./controls";
import { TrashIcon, XIcon } from "../icons";

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The user's food preferences, and their account (password, deleting it). Right after
 * sign-up it opens as onboarding: the same form, with "Skip for now". */
export function PreferencesDialog({
  config,
  user,
  onboarding,
  onSave,
  onChangePassword,
  onDeleteAccount,
  onClose,
}: {
  config: AppConfig;
  user: User;
  onboarding: boolean;
  onSave: (prefs: Preferences) => Promise<void>;
  onChangePassword: (current: string, next: string) => Promise<void>;
  onDeleteAccount: (password: string) => Promise<void>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [form, setForm] = useState<Preferences>({
    name: user.name,
    zip: user.zip,
    avoid: user.avoid,
    diet: user.diet,
    servings: user.servings,
    needs: user.needs,
  });
  const [zip, setZip] = useState(user.zip ?? "");
  const [servings, setServings] = useState(user.servings ? String(user.servings) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
  }, []);

  const zipValid = zip.trim() === "" || /^\d{5}$/.test(zip.trim());
  const servingsValid = servings.trim() === "" || (+servings >= 1 && +servings <= 24);
  const valid = form.name.trim() !== "" && zipValid && servingsValid;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      await onSave({
        ...form,
        name: form.name.trim(),
        zip: zip.trim() || null,
        servings: servings.trim() ? Number(servings) : null,
      });
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  const firstName = user.name.split(" ")[0];
  return (
    <dialog ref={dialog} className="dialog" onClose={onClose} aria-labelledby="prefs-title">
      <div className="dialog-body">
        <form onSubmit={submit} className="dialog-form">
          <header className="dialog-head">
            <div>
              <h2 id="prefs-title">{onboarding ? `Welcome, ${firstName}! Set your food preferences` : "Preferences"}</h2>
              <p className="muted">Every chat uses these. Allergies saved here are always avoided.</p>
            </div>
            <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
              <XIcon />
            </button>
          </header>

          <div className="field-row">
            <label className="field grow">
              <span>Name</span>
              <input
                value={form.name}
                maxLength={60}
                required
                autoComplete="name"
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
            <label className="field">
              <span>ZIP code</span>
              <input
                value={zip}
                inputMode="numeric"
                maxLength={5}
                placeholder="45202"
                autoComplete="postal-code"
                aria-invalid={!zipValid}
                onChange={(e) => setZip(e.target.value.replace(/\D/g, ""))}
              />
            </label>
            <label className="field">
              <span>Cooking for</span>
              <input
                value={servings}
                inputMode="numeric"
                maxLength={2}
                placeholder="4"
                aria-invalid={!servingsValid}
                onChange={(e) => setServings(e.target.value.replace(/\D/g, ""))}
              />
            </label>
          </div>

          <div className="field">
            <span>Allergies and intolerances to avoid</span>
            <ChipGroup
              label="Allergens"
              options={config.allergens}
              selected={form.avoid}
              onToggle={(id) => setForm({ ...form, avoid: toggle(form.avoid, id) })}
            />
          </div>

          <div className="field">
            <span>Diet</span>
            <Segmented
              label="Diet"
              options={config.diets}
              value={form.diet}
              onChange={(id) => setForm({ ...form, diet: id as Diet })}
            />
          </div>

          <div className="field">
            <span>
              Other needs <em className="muted">(we can't check these yet, but every list will remind you)</em>
            </span>
            <ChipGroup
              label="Other needs"
              options={config.needs}
              selected={form.needs}
              onToggle={(id) => setForm({ ...form, needs: toggle(form.needs, id) })}
            />
          </div>

          {!onboarding && <Favourites initial={user.favourites ?? {}} />}

          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}

          <footer className="dialog-foot end">
            <button type="button" className="btn ghost" onClick={onClose} disabled={busy}>
              {onboarding ? "Skip for now" : "Cancel"}
            </button>
            <button type="submit" className="btn primary" disabled={!valid || busy}>
              {busy ? "Saving…" : "Save preferences"}
            </button>
          </footer>
        </form>

        {!onboarding && (
          <Account user={user} onChangePassword={onChangePassword} onDeleteAccount={onDeleteAccount} />
        )}
      </div>
    </dialog>
  );
}

function Account({
  user,
  onChangePassword,
  onDeleteAccount,
}: {
  user: User;
  onChangePassword: (current: string, next: string) => Promise<void>;
  onDeleteAccount: (password: string) => Promise<void>;
}) {
  const [open, setOpen] = useState<"password" | "delete" | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  function show(which: "password" | "delete") {
    setOpen(open === which ? null : which);
    setMessage(null);
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onChangePassword(current, next);
      setMessage({ ok: true, text: "Password changed. Any other browser logged in to this account was logged out." });
      setCurrent("");
      setNext("");
      setOpen(null);
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  async function deleteAccount(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onDeleteAccount(confirmPassword); // on success the app returns to the login screen
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
      setBusy(false);
    }
  }

  return (
    <section className="account" aria-label="Account">
      <div className="row between wrap gap">
        <div>
          <h3>Account</h3>
          <p className="muted small">Logged in as @{user.username}</p>
        </div>
        <div className="row gap-sm wrap">
          <button type="button" className="btn ghost small" onClick={() => show("password")}>
            Change password
          </button>
          <button type="button" className="btn ghost small danger" onClick={() => show("delete")}>
            <TrashIcon size={14} /> Delete account
          </button>
        </div>
      </div>

      {open === "password" && (
        <form className="account-form" onSubmit={changePassword}>
          <label className="field">
            <span>Current password</span>
            <input
              type="password"
              value={current}
              autoComplete="current-password"
              onChange={(e) => setCurrent(e.target.value)}
            />
          </label>
          <label className="field">
            <span>New password (8 or more characters)</span>
            <input
              type="password"
              value={next}
              autoComplete="new-password"
              maxLength={128}
              onChange={(e) => setNext(e.target.value)}
            />
          </label>
          <button type="submit" className="btn primary small" disabled={busy || !current || next.length < 8}>
            {busy ? "Saving…" : "Save new password"}
          </button>
        </form>
      )}

      {open === "delete" && (
        <form className="account-form danger-zone" onSubmit={deleteAccount}>
          <p className="small">
            This deletes your account, your preferences and all your chats. It can't be undone. Enter your password
            to confirm.
          </p>
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              value={confirmPassword}
              autoComplete="current-password"
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </label>
          <button type="submit" className="btn danger-solid small" disabled={busy || !confirmPassword}>
            {busy ? "Deleting…" : "Delete my account"}
          </button>
        </form>
      )}

      {message && (
        <p className={message.ok ? "ok small" : "form-error"} role="status">
          {message.text}
        </p>
      )}
    </section>
  );
}

/** The brands remembered from the shopper's picks; any can be removed (it's asked again
 * next time). Saved at once, apart from the form's Save. */
function Favourites({ initial }: { initial: Record<string, string> }) {
  const [favourites, setFavourites] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const entries = Object.entries(favourites).sort(([a], [b]) => a.localeCompare(b));
  async function remove(item: string) {
    setError(null);
    try {
      const user = await api.setFavourite(item, null);
      setFavourites(user.favourites ?? {});
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <div className="field">
      <span>
        Your favourite brands <em className="muted">(remembered from what you pick; filled in next time)</em>
      </span>
      {entries.length === 0 ? (
        <p className="muted small">None yet: the products you pick in a build are remembered here.</p>
      ) : (
        <ul className="favourites">
          {entries.map(([item, brand]) => (
            <li key={item}>
              <span>
                <strong>{brand}</strong> <span className="muted">for {item}</span>
              </span>
              <button type="button" className="link small" onClick={() => remove(item)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

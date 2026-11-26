import { useState } from "react";
import type { AppConfig, Diet, Draft, Message, SearchRequest } from "../types";
import { ChipGroup, Segmented, toggle } from "./controls";
import { joinWords, labelOf } from "../format";
import { AlertIcon, CheckIcon, InfoIcon, SearchIcon } from "../icons";

const DIET_ORDER: Diet[] = ["none", "vegetarian", "vegan"];

function sourceHint(source: string | null | undefined): string | null {
  if (source === "profile") return "from your preferences";
  if (source === "chat") return "from your message";
  if (source === "both") return "from your message and preferences";
  return null;
}

/** The request read from the chat and the saved preferences, to check (and edit) before searching. */
export function ConfirmCard({
  message,
  config,
  actionable,
  onConfirm,
}: {
  message: Message;
  config: AppConfig;
  actionable: boolean;
  onConfirm: (request: SearchRequest, servings: number | null) => Promise<void>;
}) {
  const draft = message.data as unknown as Draft;
  const [query, setQuery] = useState(draft.request.query);
  const [zip, setZip] = useState(draft.request.zip);
  const [avoid, setAvoid] = useState<string[]>(draft.request.avoid);
  const [diet, setDiet] = useState<Diet>(draft.request.diet);
  const [servings, setServings] = useState(draft.servings ? String(draft.servings) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!actionable) {
    return (
      <div className="card confirm compact">
        <div className="row between">
          <span className="eyebrow">{draft.submitted ? "Searched" : "Replaced by your next message"}</span>
          {draft.submitted && <CheckIcon size={16} className="ok" />}
        </div>
        <p className="confirm-line">{message.text}</p>
      </div>
    );
  }

  const minRank = DIET_ORDER.indexOf(draft.min_diet ?? "none");
  const zipValid = /^\d{5}$/.test(zip);
  const servingsValid = servings === "" || (+servings >= 1 && +servings <= 24);
  const valid = query.trim() !== "" && zipValid && servingsValid;
  const avoidHint = sourceHint(
    Object.values(draft.sources.avoid).some((s) => s === "profile" || s === "both") ? "profile" : null,
  );

  async function search() {
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm({ query: query.trim(), zip, avoid, diet }, servings ? Number(servings) : null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="card confirm">
      <div className="row between">
        <h3 className="card-title">Here's what I'll search for</h3>
        {draft.state === "check" ? (
          <span className="pill warn">Please check</span>
        ) : (
          <span className="pill ok">Ready</span>
        )}
      </div>

      {draft.issues.length > 0 && (
        <ul className="callout warn" aria-label="Please check">
          {draft.issues.map((issue, i) => (
            <li key={i}>
              <AlertIcon size={16} /> {issue.text}
            </li>
          ))}
        </ul>
      )}

      <div className="field-row">
        <label className="field grow">
          <span>Dish</span>
          <input value={query} maxLength={120} onChange={(e) => setQuery(e.target.value)} disabled={busy} />
          {draft.in_catalog === false && (
            <small className="hint">Not in our dish catalog: I'll work out its ingredients first, which takes longer.</small>
          )}
        </label>
        <label className="field">
          <span>Near ZIP</span>
          <input
            value={zip}
            inputMode="numeric"
            maxLength={5}
            aria-invalid={!zipValid}
            onChange={(e) => setZip(e.target.value.replace(/\D/g, ""))}
            disabled={busy}
          />
          {zip === draft.request.zip && sourceHint(draft.sources.zip) && (
            <small className="hint">{sourceHint(draft.sources.zip)}</small>
          )}
        </label>
        <label className="field">
          <span>Servings</span>
          <input
            value={servings}
            inputMode="numeric"
            maxLength={2}
            placeholder="4"
            aria-invalid={!servingsValid}
            onChange={(e) => setServings(e.target.value.replace(/\D/g, ""))}
            disabled={busy}
          />
        </label>
      </div>

      <div className="field">
        <span>
          Avoid {avoidHint && <em className="muted">(locked ones are {avoidHint})</em>}
        </span>
        <ChipGroup
          label="Allergens to avoid"
          options={config.allergens}
          selected={avoid}
          locked={draft.locked}
          disabled={busy}
          onToggle={(id) => setAvoid(toggle(avoid, id))}
        />
      </div>

      <div className="field">
        <span>Diet</span>
        <Segmented
          label="Diet"
          options={config.diets}
          value={diet}
          disabled={busy}
          disabledIds={DIET_ORDER.filter((_, i) => i < minRank)}
          onChange={(id) => setDiet(id as Diet)}
        />
      </div>

      {(draft.notes.length > 0 || draft.needs.length > 0) && (
        <ul className="callout info">
          {draft.notes.map((note, i) => (
            <li key={i}>
              <InfoIcon size={16} /> {note}
            </li>
          ))}
          {draft.needs.length > 0 && (
            <li>
              <InfoIcon size={16} /> We can't check{" "}
              {joinWords(draft.needs.map((n) => labelOf(config.needs, n).toLowerCase()))} yet. The list will
              remind you to confirm it yourself.
            </li>
          )}
        </ul>
      )}

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <div className="row between wrap gap">
        <small className="muted">Store data is checked live and is never saved.</small>
        <button className="btn primary" onClick={search} disabled={!valid || busy}>
          <SearchIcon size={16} /> {busy ? "Starting…" : "Search stores"}
        </button>
      </div>
    </div>
  );
}

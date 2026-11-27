import { useState } from "react";
import { api } from "../api";
import type { AppConfig, Diet, Draft, Message, SearchRequest } from "../types";
import { ChipGroup, Segmented, toggle } from "./controls";
import { aisleText, joinWords, labelOf } from "../format";
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
  onAddMore,
}: {
  message: Message;
  config: AppConfig;
  actionable: boolean;
  onConfirm: (request: SearchRequest, servings: number | null) => Promise<void>;
  onAddMore?: (text: string) => Promise<void>; // a build's summary: "Anything else to add?"
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
          <input
            value={query}
            maxLength={120}
            onChange={(e) => setQuery(e.target.value)}
            disabled={busy || !!draft.guided}
            title={draft.guided ? "Chosen in your build: start over to change the dish" : undefined}
          />
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
            disabled={busy || !!draft.guided}
            title={draft.guided ? "Your products come from this ZIP's stores: start over to change it" : undefined}
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

      {draft.guided && (
        <small className="hint">
          The dish and ZIP come from your build. Changing allergies or diet here searches your items afresh.
        </small>
      )}

      {draft.guided && (
        <div className="field">
          <span>
            Your products <em className="muted">({draft.guided.ingredients.length})</em>
          </span>
          <ul className="summary-products">
            {draft.guided.ingredients.map((i) => {
              const p = draft.guided?.choices?.[i.name];
              return (
                <li key={i.name} className={draft.guided?.added.includes(i.name) ? "added" : ""}>
                  <span className="summary-item">{i.name}</span>
                  {p ? (
                    <span className="summary-product">
                      <span className={`verdict ${p.verdict}`}>{p.verdict === "safe" ? "Safe" : "Read the label"}</span>{" "}
                      {p.name}
                      <span className="muted small">
                        {" "}
                        · {p.store}
                        {p.aisle ? ` · ${aisleText(p.aisle)}` : ""}
                      </span>
                    </span>
                  ) : (
                    <span className="muted small">product found when you search</span>
                  )}
                </li>
              );
            })}
          </ul>
          {Object.keys(draft.guided.favourites_new ?? {}).length > 0 && (
            <small className="hint">
              New favourites, remembered when you search:{" "}
              {Object.entries(draft.guided.favourites_new ?? {})
                .map(([item, brand]) => `${brand} for ${item}`)
                .join(", ")}
              .
            </small>
          )}
          <FavouriteQuestions asks={draft.guided.favourites_ask ?? []} />
        </div>
      )}

      {draft.guided && onAddMore && <AddMore onAdd={onAddMore} disabled={busy} />}

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

/** "Make Nature's Own your usual sandwich bread instead of Dave's Killer Bread?" A favourite
 * is never replaced without the shopper saying so. */
function FavouriteQuestions({ asks }: { asks: { item: string; old: string; new: string }[] }) {
  const [answered, setAnswered] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  if (!asks.length) return null;
  async function answer(item: string, brand: string | null, label: string) {
    setError(null);
    try {
      if (brand) await api.setFavourite(item, brand);
      setAnswered((a) => ({ ...a, [item]: label }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <ul className="favourite-asks">
      {asks.map((a) => (
        <li key={a.item}>
          {answered[a.item] ? (
            <span className="muted small">{answered[a.item]}</span>
          ) : (
            <>
              <span className="small">
                Make <strong>{a.new}</strong> your usual {a.item} instead of {a.old}?
              </span>
              <span className="row gap">
                <button className="btn small primary" onClick={() => answer(a.item, a.new, `${a.new} is your usual ${a.item} now.`)}>
                  Yes
                </button>
                <button className="btn small ghost" onClick={() => answer(a.item, null, `Kept ${a.old}.`)}>
                  Keep {a.old}
                </button>
              </span>
            </>
          )}
        </li>
      ))}
      {error && <li className="form-error">{error}</li>}
    </ul>
  );
}

/** At a build's summary: "Anything else to add?" Adding opens the build again with the
 * items (searched and checked like any addition); Review makes a new summary. */
function AddMore({ onAdd, disabled }: { onAdd: (text: string) => Promise<void>; disabled: boolean }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function add(event: React.FormEvent) {
    event.preventDefault();
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      await onAdd(value);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <div className="field add-more">
      <span>Anything else to add?</span>
      <form className="add-own" onSubmit={add}>
        <input
          value={text}
          maxLength={300}
          placeholder="ketchup, pickles, Dave's Killer Bread…"
          onChange={(e) => setText(e.target.value)}
          disabled={disabled || busy}
          aria-label="Add more items"
        />
        <button type="submit" className="btn small ghost" disabled={disabled || busy || !text.trim()}>
          {busy ? "Adding…" : "Add"}
        </button>
      </form>
      <small className="hint">Or search now. Added items get products and an allergy check, then you review again.</small>
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

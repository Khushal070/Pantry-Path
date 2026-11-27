import { useState, type FormEvent } from "react";
import type { BuildAction, BuildData, Message, ProductChoice, ProductView } from "../types";
import { aisleText, cap, joinWords, milesText } from "../format";
import { AlertIcon, CheckIcon, ChevronIcon, InfoIcon, PlusIcon, SearchIcon } from "../icons";

/**
 * A guided build (backend/app/guided.py): one step at a time, the usual options ticked,
 * an "add your own" box, and clashes with the shopper's allergies or diet to decide.
 * Rendered with a key that changes with the step and its picks, so its local state
 * starts again whenever the server moves the build on.
 */
export function BuildCard({
  message,
  actionable,
  onAction,
}: {
  message: Message;
  actionable: boolean;
  onAction: (body: BuildAction) => Promise<void>;
}) {
  const build = message.data as unknown as BuildData;
  const { plan } = build;
  const step = plan.steps[build.step];
  const [picked, setPicked] = useState<string[]>(step ? (build.picks[step.id] ?? []) : []);
  const [extra, setExtra] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!actionable || build.state !== "choosing" || !step) {
    const label =
      build.state === "done"
        ? "Chosen"
        : build.state === "searched"
          ? "Searched"
          : build.state === "cancelled"
            ? "Started over"
            : "Replaced by your next message";
    const chosen = plan.steps.flatMap((s) => build.picks[s.id] ?? []);
    const added = build.additions.filter((a) => a.status !== "clash").map((a) => a.name);
    return (
      <div className="card confirm compact">
        <div className="row between">
          <span className="eyebrow">{label}</span>
          {(build.state === "done" || build.state === "searched") && <CheckIcon size={16} className="ok" />}
        </div>
        <p className="confirm-line">
          {cap(plan.dish)}
          {chosen.length + added.length > 0 && `: ${joinWords([...chosen, ...added])}`}
        </p>
      </div>
    );
  }

  const last = build.step === plan.steps.length - 1;
  const mainStore = build.products.stores.find((st) => st.store_id === build.products.main);
  const clashes = build.additions.filter((a) => a.status === "clash");

  async function run(body: BuildAction) {
    setBusy(true);
    setError(null);
    try {
      await onAction(body);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  function toggle(name: string) {
    setPicked((now) => (now.includes(name) ? now.filter((n) => n !== name) : [...now, name]));
  }

  function add(event: FormEvent) {
    event.preventDefault();
    const text = extra.trim();
    if (!text) return;
    setExtra("");
    run({ action: "add", text });
  }

  return (
    <div className="card build">
      <div className="row between wrap gap">
        <h3 className="card-title">Your {plan.dish}</h3>
        <span className="eyebrow">
          Step {build.step + 1} of {plan.steps.length}
        </span>
      </div>
      {mainStore && (
        <p className="muted small main-store">
          Shopping at <strong>{mainStore.name}</strong> ({milesText(mainStore.distance_miles)}): the store with safe
          products for most of your items.
        </p>
      )}
      <ol className="step-dots" aria-hidden="true">
        {plan.steps.map((s, i) => (
          <li key={s.id} className={i < build.step ? "done" : i === build.step ? "now" : ""} />
        ))}
      </ol>

      {build.products.status === "loading" && (
        <p className="muted small finding">
          <span className="dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>{" "}
          Finding products at stores near {build.draft.request.zip}…
        </p>
      )}
      {build.products.status === "failed" && (
        <p className="callout warn small">
          <AlertIcon size={16} /> {build.products.error ?? "Products couldn't be found right now."} You can still
          choose; products are searched again at the end.
        </p>
      )}

      <div className="field">
        <span>{step.label}</span>
        <ul className="option-rows" aria-label={step.label}>
          {step.options.map((o) => {
            const on = picked.includes(o.name);
            return (
              <li key={o.name} className={`option-row${on ? " on" : ""}`}>
                <button
                  type="button"
                  className="option-toggle"
                  aria-pressed={on}
                  disabled={busy}
                  onClick={() => toggle(o.name)}
                >
                  <span className="box" aria-hidden="true">
                    {on && <CheckIcon size={12} />}
                  </span>
                  <span className="option-name">{o.name}</span>
                  {o.share ? <small className="chip-note">{Math.round(o.share * 100)}% of recipes</small> : null}
                  {o.instead_of && <small className="chip-note">for {o.instead_of}</small>}
                </button>
                {on && (
                  <ProductPicker
                    name={o.name}
                    choice={build.choices[o.name]}
                    chosenKey={build.chosen[o.name]}
                    loading={build.products.status === "loading"}
                    busy={busy}
                    onChoose={(key) => run({ action: "choose", name: o.name, key })}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {step.left_out.length > 0 && (
        <p className="callout info small">
          <InfoIcon size={16} /> Left out:{" "}
          {step.left_out.map((x, i) => (
            <span key={x.name}>
              {i > 0 && "; "}
              {x.name} ({x.reason})
            </span>
          ))}
        </p>
      )}

      {build.additions.length > 0 && (
        <div className="field">
          <span>Your additions</span>
          <ul className="additions">
            {build.additions.map((a) => (
              <li key={a.name} className={a.status === "clash" ? "clash" : ""}>
                <strong>{a.name}</strong>
                {a.instead_of && <span className="muted small"> instead of {a.instead_of}</span>}
                {a.status === "anyway" && <span className="pill warn">searching anyway</span>}
                {a.status !== "clash" && (
                  <ProductPicker
                    name={a.name}
                    choice={build.choices[a.name]}
                    chosenKey={build.chosen[a.name]}
                    loading={!build.choices[a.name]}
                    busy={busy}
                    onChoose={(key) => run({ action: "choose", name: a.name, key })}
                  />
                )}
                {a.status === "clash" && a.clash && (
                  <>
                    <span className="clash-reason">
                      <AlertIcon size={14} /> {cap(a.clash.reason)}.
                      {a.clash.swap ? ` Try ${a.clash.swap} instead?` : ""}
                    </span>
                    <span className="row wrap gap">
                      {a.clash.swap && (
                        <button
                          className="btn small primary"
                          disabled={busy}
                          onClick={() => run({ action: "resolve", name: a.name, choice: "swap" })}
                        >
                          Use {a.clash.swap}
                        </button>
                      )}
                      <button
                        className="btn small ghost"
                        disabled={busy}
                        onClick={() => run({ action: "resolve", name: a.name, choice: "anyway" })}
                      >
                        Search {a.name} anyway
                      </button>
                      <button
                        className="btn small ghost"
                        disabled={busy}
                        onClick={() => run({ action: "resolve", name: a.name, choice: "remove" })}
                      >
                        Remove
                      </button>
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <form className="add-own" onSubmit={add}>
        <input
          value={extra}
          maxLength={300}
          placeholder="Add your own: pineapple, Dave's Killer Bread…"
          onChange={(e) => setExtra(e.target.value)}
          disabled={busy}
          aria-label="Add your own items"
        />
        <button type="submit" className="btn small ghost" disabled={busy || !extra.trim()}>
          <PlusIcon size={14} /> Add
        </button>
      </form>

      {clashes.length > 0 && (
        <p className="callout warn small">
          <AlertIcon size={16} /> Decide about {joinWords(clashes.map((c) => c.name))} before searching.
        </p>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <div className="row between wrap gap">
        <span className="row wrap gap">
          <button className="btn small ghost" disabled={busy} onClick={() => run({ action: "cancel" })}>
            Start over
          </button>
          <button
            className="btn small ghost"
            disabled={busy || clashes.length > 0}
            title="Search now with the usual options for the steps left"
            onClick={() => run({ action: "usual", picks: picked })}
          >
            <SearchIcon size={14} /> {build.step === 0 ? "Use the usual" : "Usual for the rest"}
          </button>
        </span>
        <span className="row gap">
          {build.step > 0 && (
            <button className="btn ghost" disabled={busy} onClick={() => run({ action: "back", picks: picked })}>
              Back
            </button>
          )}
          <button
            className="btn primary"
            disabled={busy || (last && clashes.length > 0)}
            onClick={() => run({ action: "next", picks: picked })}
          >
            {last ? "Review" : "Next"} <ChevronIcon size={14} />
          </button>
        </span>
      </div>
      <small className="muted">
        Store data is fetched in the background while you choose, and is never saved.
      </small>
    </div>
  );
}

/** An option's product: the pick (favourite marked), and on "Change" the family brand
 * first, then the alternatives. Unsafe products are never offered (counted). */
function ProductPicker({
  name,
  choice,
  chosenKey,
  loading,
  busy,
  onChoose,
}: {
  name: string;
  choice: ProductChoice | undefined;
  chosenKey: string | undefined;
  loading: boolean;
  busy: boolean;
  onChoose: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!choice) return loading ? <p className="product-line muted small">Finding {name}…</p> : null;
  const all = [choice.pick, choice.hint, ...choice.alternatives].filter(Boolean) as ProductView[];
  const chosen = all.find((v) => v.key === chosenKey) ?? choice.pick;
  if (!chosen) {
    return (
      <p className="product-line muted small">
        {choice.error ?? `No product for ${name} near you that we could offer`}
        {choice.hidden_unsafe ? ` (${choice.hidden_unsafe} not safe for you, hidden)` : ""}.
      </p>
    );
  }
  const isFavourite = !!choice.favourite && !choice.favourite_missing && chosen.key === choice.pick?.key;
  const others = all.filter((v) => v.key !== chosen.key);
  return (
    <div className="product-pick">
      <ProductLine view={chosen} badge={isFavourite ? "Your usual" : null} />
      {choice.favourite_missing && (
        <p className="muted small">Your usual {choice.favourite} isn't at these stores: here's the closest safe one.</p>
      )}
      {others.length > 0 && (
        <button type="button" className="link small" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Keep this one" : `Change (${others.length} more)`}
        </button>
      )}
      {open && (
        <ul className="product-alternatives">
          {others.map((v) => (
            <li key={v.key}>
              <button
                type="button"
                className="product-choice"
                disabled={busy}
                onClick={() => {
                  setOpen(false);
                  onChoose(v.key);
                }}
              >
                <ProductLine
                  view={v}
                  badge={choice.hint && v.key === choice.hint.key ? `Your usual ${choice.hint_brand ?? ""} brand` : null}
                />
              </button>
            </li>
          ))}
          {!!choice.hidden_unsafe && (
            <li className="muted small">{choice.hidden_unsafe} more not safe for you, hidden.</li>
          )}
        </ul>
      )}
    </div>
  );
}

function ProductLine({ view, badge }: { view: ProductView; badge: string | null }) {
  return (
    <span className="product-line">
      <span className={`verdict ${view.verdict}`}>{view.verdict === "safe" ? "Safe" : "Read the label"}</span>
      <span className="product-text">
        <strong>{view.name}</strong>
        {badge && <span className="pill ok small-pill">★ {badge}</span>}
        <span className="muted small">
          {[view.size, aisleText(view.aisle), view.other_store && `only at ${view.store}`]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </span>
    </span>
  );
}

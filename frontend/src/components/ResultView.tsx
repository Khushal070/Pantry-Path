import { useEffect, useState } from "react";
import type { Job, Result, SavedItem, SavedList } from "../types";
import { clockTime } from "../format";
import { CheckIcon, RefreshIcon } from "../icons";
import { announceListsChanged, deviceUser, loadList, tick } from "../offline";
import { ListView } from "./ListView";

/** The local model works out the ingredients of a dish the catalog doesn't have, and can
 * get regional dishes wrong. The shopper can ask a stronger model to redo them: a new
 * search appears below, and this answer stays. Once redone, the result says which model. */
function StrongerModelOffer({
  job,
  stronger,
}: {
  job: Job;
  stronger: { label: string; onAsk: () => Promise<void> } | null;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const used = job.model_used;
  if (used?.stronger) {
    return <span className="tag stronger-tag">Ingredients by {used.a2 ?? "a stronger model"}</span>;
  }
  if (!stronger || (job.result as Result).dish.source === "catalog") return null;

  async function ask() {
    if (!stronger) return;
    setBusy(true);
    setError(null);
    try {
      await stronger.onAsk();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stronger">
      <button className="btn ghost small" onClick={ask} disabled={busy}>
        <RefreshIcon size={14} /> {busy ? "Starting…" : "Not right? Ask a stronger model"}
      </button>
      <small className="muted">
        Sends only the dish name to {stronger.label}. Store data is never shared.
      </small>
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}


/** A finished search, list first (ListView): its saved list (My lists) with the live
 * products while they're in memory. Without a saved list (saving failed) the same list is
 * built here from the answer, with ticks for this page only. */
export function ResultView({
  job,
  mode,
  stronger = null,
}: {
  job: Job;
  mode: "demo" | "live";
  // "Not right? Ask a stronger model": offered when a model worked out the ingredients
  stronger?: { label: string; onAsk: () => Promise<void> } | null;
}) {
  const result = job.result as Result;
  const [list, setList] = useState<SavedList | null>(null);
  const [local, setLocal] = useState<SavedList>(() => listFromJob(job));
  const [tickError, setTickError] = useState<string | null>(null);

  useEffect(() => {
    if (!job.list_id) return;
    let stopped = false;
    loadList(deviceUser(), job.list_id)
      .then(({ list: saved }) => {
        if (!stopped) setList(saved);
        announceListsChanged();
      })
      .catch(() => undefined); // the list built from the answer shows instead
    return () => {
      stopped = true;
    };
  }, [job.list_id]);

  async function onTick(item: SavedItem) {
    setTickError(null);
    if (!list) {
      setLocal((l) => ({ ...l, items: l.items.map((i) => (i.id === item.id ? { ...i, ticked: !i.ticked } : i)) }));
      return;
    }
    try {
      setList(await tick(deviceUser(), list, item.id, !item.ticked));
      announceListsChanged();
    } catch (e) {
      setTickError(e instanceof Error ? e.message : "Couldn't save that tick.");
    }
  }

  const attribution = job.warnings.filter((w) => w.startsWith("Product data:"));
  const notes = job.warnings.filter((w) => !w.startsWith("Product data:") && !w.includes("couldn't verify"));
  const notice = list ? (
    <span className="pill ok">
      <CheckIcon size={12} /> Saved to My lists
    </span>
  ) : job.list_error ? (
    <span className="pill warn">Couldn't save this list</span>
  ) : null;

  return (
    <div className="result-wrap">
      <StrongerModelOffer job={job} stronger={stronger} />
      <ListView
        list={list ?? local}
        result={result}
        answer={job.answer}
        mode={mode}
        onTick={onTick}
        notice={notice}
        summary={job.answer?.summary}
        choices={job.choices}
      />
      {tickError && (
        <p className="form-error" role="alert">
          {tickError}
        </p>
      )}
      <p className="footnote">
        {[
          ...notes,
          ...attribution,
          job.expires_at && `Store details expire at ${clockTime(job.expires_at)} and are never saved; your list is.`,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
  );
}

/** The list the backend would save (lists.from_search), for when it couldn't be saved. */
function listFromJob(job: Job): SavedList {
  const result = job.result as Result;
  const category = new Map(result.ingredients.map((i) => [i.name, i.category]));
  const items: SavedItem[] = job.answer?.ingredients.length
    ? job.answer.ingredients.map((a, n) => ({
        id: `i${n + 1}`,
        name: a.name,
        category: category.get(a.name) ?? "other",
        status: a.status,
        substitute: a.substitute,
        quantity: a.quantity,
        unit: a.unit,
        ticked: false,
      }))
    : result.ingredients.map((i, n) => ({
        id: `i${n + 1}`,
        name: i.name,
        category: i.category,
        status: i.products.some((p) => p.verdict === "safe") ? "buy" : "check_label",
        substitute: null,
        quantity: null,
        unit: null,
        ticked: false,
      }));
  return {
    id: "",
    dish: result.dish.name,
    cuisine: result.dish.cuisine,
    zip: result.request.zip,
    avoid: result.request.avoid,
    diet: result.request.diet,
    servings: job.answer?.servings ?? null,
    items,
    steps: job.answer?.steps ?? [],
    source: "search",
    created_at: job.created_at,
    updated_at: null,
  };
}

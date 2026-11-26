import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Answer, AnswerItem, Job, Product, Result, Store, Verdict } from "../types";
import {
  CUISINE_LABEL,
  EVIDENCE_LABEL,
  VERDICT_LABEL,
  cap,
  clockTime,
  milesText,
  quantityText,
  routeLink,
  storeLink,
} from "../format";
import { AlertIcon, CheckIcon, QuestionIcon, RefreshIcon, RouteIcon, StoreIcon, XIcon } from "../icons";

type Tab = "list" | "stores" | "products" | "steps";
const VERDICT_ORDER: Record<Verdict, number> = { safe: 0, unknown: 1, unsafe: 2 };

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

export function ResultView({
  job,
  mode,
  listKey,
  stronger = null,
}: {
  job: Job;
  mode: "demo" | "live";
  listKey: string; // where this search's ticked items are kept
  // "Not right? Ask a stronger model": offered when a model worked out the ingredients
  stronger?: { label: string; onAsk: () => Promise<void> } | null;
}) {
  const result = job.result as Result;
  const answer = job.answer;
  const [tab, setTab] = useState<Tab>(answer ? "list" : "products");
  const [got, toggle] = useTicked(listKey);
  const storeById = useMemo(() => new Map(result.stores.map((s) => [s.store_id, s])), [result]);
  const { with_safe_product: safe, ingredients_total: total } = result.summary;
  const attribution = job.warnings.filter((w) => w.startsWith("Product data:"));
  const warnings = job.warnings.filter((w) => !w.startsWith("Product data:"));

  const tabs: { id: Tab; label: string }[] = [
    ...(answer ? [{ id: "list" as Tab, label: "Shopping list" }] : []),
    { id: "stores", label: `Stores (${result.stores.length})` },
    { id: "products", label: "Every product checked" },
    ...(answer && answer.steps.length ? [{ id: "steps" as Tab, label: "Steps" }] : []),
  ];

  return (
    <div className="card result">
      <div className="result-head">
        <div>
          <div className="eyebrow">
            {CUISINE_LABEL[result.dish.cuisine] ?? result.dish.cuisine} ·{" "}
            {result.dish.source === "llm" ? "ingredients worked out for this dish" : "from our dish catalog"}
          </div>
          <h3 className="result-title">{cap(result.dish.name)}</h3>
          <StrongerModelOffer job={job} stronger={stronger} />
        </div>
        <div className="meter" title={`${safe} of ${total} ingredients have a safe product`}>
          <div className="meter-label">
            <strong>{safe}</strong>/{total} ingredients safe to buy
          </div>
          <div className="meter-bar">
            <span style={{ width: `${total ? (100 * safe) / total : 0}%` }} />
          </div>
        </div>
      </div>

      <p className="summary">
        {answer?.summary ??
          `${safe} of ${total} ingredients have a product marked safe at a nearby store.` +
            (result.summary.without_safe_product.length
              ? ` No safe product for ${result.summary.without_safe_product.join(", ")}.`
              : "")}
      </p>
      {!answer && (
        <p className="callout info small">
          The shopping-list writer isn't available right now, so here is every product as it was checked.
        </p>
      )}

      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={tab === t.id ? "on" : ""}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="tab-panel" role="tabpanel">
        {tab === "list" && answer && (
          <ShoppingList answer={answer} result={result} storeById={storeById} got={got} toggle={toggle} />
        )}
        {tab === "stores" && <Stores result={result} answer={answer} mode={mode} />}
        {tab === "products" && <Products result={result} storeById={storeById} />}
        {tab === "steps" && answer && (
          <ol className="steps-list">
            {answer.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        )}
      </div>

      {warnings.length > 0 && (
        <ul className="callout warn">
          {warnings.map((w, i) => (
            <li key={i}>
              <AlertIcon size={16} /> {w}
            </li>
          ))}
        </ul>
      )}
      <p className="footnote">
        {[...attribution, job.expires_at && `These results expire at ${clockTime(job.expires_at)}: store data is never saved.`]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
  );
}

function productFor(result: Result, item: AnswerItem): Product | undefined {
  return result.ingredients
    .find((i) => i.name === item.name)
    ?.products.find((p) => p.name === item.product && p.store_id === item.store_id);
}

/** The items ticked off one search's shopping list. They live here, above the tabs, so
 * switching tabs keeps them, and in this browser's storage, so switching chats or
 * reloading keeps them too. Storage can be off (a private window): then they last as long
 * as the page. */
function useTicked(key: string): [Set<string>, (name: string) => void] {
  const [ticked, setTicked] = useState<Set<string>>(() => {
    try {
      return new Set(JSON.parse(window.localStorage.getItem(key) ?? "[]") as string[]);
    } catch {
      return new Set();
    }
  });
  useEffect(() => {
    try {
      if (ticked.size) window.localStorage.setItem(key, JSON.stringify([...ticked]));
      else window.localStorage.removeItem(key);
    } catch {
      // storage is off: the ticks stay for this page only
    }
  }, [key, ticked]);
  const toggle = (name: string) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  return [ticked, toggle];
}

function ShoppingList({
  answer,
  result,
  storeById,
  got,
  toggle,
}: {
  answer: Answer;
  result: Result;
  storeById: Map<string, Store>;
  got: Set<string>;
  toggle: (name: string) => void;
}) {
  const groups = new Map<string, AnswerItem[]>();
  for (const item of answer.ingredients) {
    if (item.status === "buy" && item.store_id) groups.set(item.store_id, [...(groups.get(item.store_id) ?? []), item]);
  }
  const byStore = [...groups.entries()]
    .map(([id, items]) => ({ store: storeById.get(id), items }))
    .sort((a, b) => (a.store?.distance_miles ?? 99) - (b.store?.distance_miles ?? 99));
  const of = (status: AnswerItem["status"]) => answer.ingredients.filter((i) => i.status === status);

  return (
    <div className="shopping">
      {byStore.map(({ store, items }) => (
        <section className="list-group" key={store?.store_id ?? "?"}>
          <h4>
            <StoreIcon size={16} /> {store?.name ?? "Store"}{" "}
            {store && <span className="muted">{milesText(store.distance_miles)}</span>}
          </h4>
          <ul className="items">
            {items.map((item) => {
              const product = productFor(result, item);
              const details = [product?.size, product?.aisle && `${product.aisle} aisle`].filter(Boolean);
              return (
                <li key={item.name} className={got.has(item.name) ? "got" : ""}>
                  <label className="item">
                    <input type="checkbox" checked={got.has(item.name)} onChange={() => toggle(item.name)} />
                    <span className="item-main">
                      <span className="item-name">
                        {cap(item.name)}
                        {quantityText(item) && <span className="qty">{quantityText(item)}</span>}
                      </span>
                      <span className="item-product">
                        {item.product}
                        {details.length > 0 && <span className="muted"> · {details.join(" · ")}</span>}
                      </span>
                      {product?.in_stock === false && <span className="tag warn">Out of stock at this store</span>}
                      {item.note && <span className="item-note">{item.note}</span>}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        </section>
      ))}

      <Group title="Use a substitute" items={of("substitute")} render={(item) => (
        <>
          <span className="item-name">
            {cap(item.name)} {item.substitute && <>→ <strong>{item.substitute}</strong></>}
            {quantityText(item) && <span className="qty">{quantityText(item)}</span>}
          </span>
          {item.note && <span className="item-note">{item.note}</span>}
        </>
      )} />
      <Group title="Read the label before buying" items={of("check_label")} render={(item) => (
        <>
          <span className="item-name">{cap(item.name)}</span>
          {item.note && <span className="item-note">{item.note}</span>}
        </>
      )} />
      <Group title="Leave out" items={of("skip")} render={(item) => (
        <>
          <span className="item-name">{cap(item.name)}</span>
          {item.note && <span className="item-note">{item.note}</span>}
        </>
      )} />
    </div>
  );
}

function Group({
  title,
  items,
  render,
}: {
  title: string;
  items: AnswerItem[];
  render: (item: AnswerItem) => ReactNode;
}) {
  if (!items.length) return null;
  return (
    <section className="list-group">
      <h4>{title}</h4>
      <ul className="items plain">
        {items.map((item) => (
          <li key={item.name}>
            <span className="item-main">{render(item)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Stores({ result, answer, mode }: { result: Result; answer: Answer | null; mode: "demo" | "live" }) {
  const stores = [...result.stores].sort((a, b) => a.distance_miles - b.distance_miles);
  const buying = (id: string) => answer?.ingredients.filter((i) => i.status === "buy" && i.store_id === id).length ?? 0;
  const safeHere = (id: string) =>
    result.ingredients.filter((i) => i.products.some((p) => p.store_id === id && p.verdict === "safe")).length;
  const stops = answer ? stores.filter((s) => buying(s.store_id) > 0) : stores;
  const route = mode === "live" ? routeLink(result.request.zip, stops) : null;
  return (
    <div className="stores">
      {route && (
        <a className="btn ghost" href={route} target="_blank" rel="noreferrer">
          <RouteIcon size={16} /> Open a route through {stops.length} store{stops.length === 1 ? "" : "s"}
        </a>
      )}
      {stores.map((s) => (
        <div className="store-card" key={s.store_id}>
          <div className="row between">
            <strong>{s.name}</strong>
            <span className="muted small">{milesText(s.distance_miles)}</span>
          </div>
          <div className="muted small">{s.address}</div>
          <div className="row gap wrap small">
            {answer && <span className="tag">{buying(s.store_id)} to buy here</span>}
            <span className="tag">
              Safe options for {safeHere(s.store_id)} of {result.ingredients.length} ingredients
            </span>
            {mode === "live" && (
              <a href={storeLink(s)} target="_blank" rel="noreferrer">
                Map
              </a>
            )}
          </div>
        </div>
      ))}
      {mode === "demo" && <p className="muted small">Demo stores are synthetic, so there are no map links.</p>}
    </div>
  );
}

function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const Icon = verdict === "safe" ? CheckIcon : verdict === "unsafe" ? XIcon : QuestionIcon;
  return (
    <span className={`verdict ${verdict}`}>
      <Icon size={12} /> {VERDICT_LABEL[verdict]}
    </span>
  );
}

function stockText(inStock: boolean | null): string {
  return inStock === true ? "in stock" : inStock === false ? "out of stock" : "stock not reported";
}

function Products({ result, storeById }: { result: Result; storeById: Map<string, Store> }) {
  return (
    <div className="products">
      <p className="muted small">
        Every product found, judged by our diet rules (not by the AI). "Couldn't verify" means no full ingredient
        list: treat it as not safe.
      </p>
      {result.ingredients.map((ing) => {
        const count = (v: Verdict) => ing.products.filter((p) => p.verdict === v).length;
        const products = [...ing.products].sort(
          (a, b) =>
            VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] ||
            (storeById.get(a.store_id)?.distance_miles ?? 99) - (storeById.get(b.store_id)?.distance_miles ?? 99),
        );
        return (
          <details className="ingredient" key={ing.name}>
            <summary>
              <span className="ing-name">{cap(ing.name)}</span>
              <span className={`role ${ing.role}`}>{ing.role === "core" ? "key ingredient" : "optional"}</span>
              <span className="counts">
                {count("safe") > 0 && <span className="count safe">{count("safe")} safe</span>}
                {count("unknown") > 0 && <span className="count unknown">{count("unknown")} unverified</span>}
                {count("unsafe") > 0 && <span className="count unsafe">{count("unsafe")} not safe</span>}
                {ing.products.length === 0 && <span className="count">none found</span>}
              </span>
            </summary>
            <ul className="product-list">
              {products.map((p, i) => (
                <li key={`${p.store_id}-${p.name}-${i}`} className="product">
                  <VerdictBadge verdict={p.verdict} />
                  <div className="product-main">
                    <div className="product-name">{p.name}</div>
                    <div className="muted small">
                      {[p.brand, p.size, storeById.get(p.store_id)?.name, stockText(p.in_stock)]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                    <div className="small">
                      {p.reason} <span className="muted">({EVIDENCE_LABEL[p.evidence_source]})</span>
                    </div>
                  </div>
                </li>
              ))}
              {products.length === 0 && <li className="muted small">No products found at nearby stores.</li>}
            </ul>
          </details>
        );
      })}
    </div>
  );
}

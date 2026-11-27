import { useMemo, useState, type ReactNode } from "react";
import type { Answer, Product, ProductView, Result, SavedItem, SavedList, Store } from "../types";
import { aisleText, cap, milesText } from "../format";
import { AlertIcon, CheckIcon, ChevronIcon, QuestionIcon, RefreshIcon } from "../icons";
import { Products, Stores, VERDICT_ORDER, VerdictBadge } from "./Panels";

/** Store sections, from our own ingredient categories (Kroger's aisles can't be saved). */
const SECTIONS: { id: string; label: string; categories: string[] }[] = [
  { id: "produce", label: "Produce", categories: ["produce"] },
  { id: "dairy", label: "Dairy & eggs", categories: ["dairy", "egg"] },
  { id: "meat", label: "Meat & seafood", categories: ["meat", "seafood"] },
  { id: "bakery", label: "Bakery & grains", categories: ["bakery", "grain"] },
  { id: "pantry", label: "Pantry", categories: [] }, // everything else
];

type Panel = "steps" | "stores" | "products" | null;

export function quantity(item: SavedItem): string {
  if (item.quantity == null) return "";
  const amount = Number.isInteger(item.quantity) ? String(item.quantity) : item.quantity.toFixed(2).replace(/0+$/, "");
  return item.unit ? `${amount} ${item.unit}` : amount;
}

// What the item was searched as: its substitute, or "brand item" for a picked brand (as
// "Find products again" searches it; backend chat.find_products)
const searchedName = (item: SavedItem) =>
  item.status === "substitute" && item.substitute
    ? item.substitute
    : item.brand
      ? `${item.brand} ${item.name}`
      : item.name;

/**
 * A shopping list, list first (docs/superpowers/specs/2026-11-27-saved-lists-design.md):
 * sections by store area, a status per item, tap a row to tick it, tap ⌄ for its products
 * and why. Steps, stores and every product sit behind buttons. Used right after a search
 * (with its live result) and for a saved list (products after "Find products again").
 */
export function ListView({
  list,
  result,
  answer,
  mode,
  onTick,
  notice,
  actions,
  summary,
  choices = null,
}: {
  list: SavedList;
  result: Result | null; // live store data, when there is some
  answer?: Answer | null;
  mode: "demo" | "live";
  onTick: (item: SavedItem) => void;
  notice?: ReactNode; // e.g. "Saved to My lists", "Offline: your saved copy"
  actions?: ReactNode; // e.g. Find products again
  summary?: string | null;
  choices?: Record<string, ProductView> | null; // the shopper's picks, while in memory
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const storeById = useMemo(() => new Map((result?.stores ?? []).map((s) => [s.store_id, s])), [result]);

  const shop = list.items.filter((i) => i.status !== "skip");
  const skip = list.items.filter((i) => i.status === "skip");
  const ticked = shop.filter((i) => i.ticked).length;
  const toCheck = shop.filter((i) => i.status === "check_label").length;
  const swaps = shop.filter((i) => i.status === "substitute").length;
  const known = new Set(SECTIONS.flatMap((s) => s.categories));
  const sections = SECTIONS.map((s) => ({
    ...s,
    items: shop
      .filter((i) => (s.categories.length ? s.categories.includes(i.category) : !known.has(i.category)))
      // ticked items sink to the bottom of their section
      .sort((a, b) => Number(a.ticked) - Number(b.ticked)),
  })).filter((s) => s.items.length);

  const products = (item: SavedItem): { product: Product; store: Store | undefined }[] => {
    const ingredient =
      result?.ingredients.find((i) => i.name === searchedName(item)) ??
      result?.ingredients.find((i) => i.name === item.name);
    return [...(ingredient?.products ?? [])]
      .sort(
        (a, b) =>
          VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] ||
          (storeById.get(a.store_id)?.distance_miles ?? 99) - (storeById.get(b.store_id)?.distance_miles ?? 99),
      )
      .slice(0, 3)
      .map((p) => ({ product: p, store: storeById.get(p.store_id) }));
  };

  const togglePanel = (p: Panel) => setPanel((now) => (now === p ? null : p));

  return (
    <div className="card result list-first">
      <header className="list-head">
        <div className="row between wrap gap">
          <h3 className="result-title">
            {cap(list.dish)}
            {list.servings ? <span className="muted"> · for {list.servings}</span> : null}
          </h3>
          {notice}
        </div>
        <p className="list-counts">
          <strong>{shop.length}</strong> items
          {toCheck > 0 && <> · <span className="unknown-text">{toCheck} read the label</span></>}
          {swaps > 0 && <> · {swaps} swap{swaps === 1 ? "" : "s"}</>}
          <span className="list-progress">
            {ticked}/{shop.length} in the basket
          </span>
        </p>
        <div className="progress-bar" aria-hidden="true">
          <span style={{ width: `${shop.length ? (100 * ticked) / shop.length : 0}%` }} />
        </div>
        {toCheck > 0 && (
          <p className="callout warn small">
            <AlertIcon size={16} /> {toCheck} item{toCheck === 1 ? "" : "s"} couldn't be fully checked: read the
            label before buying (marked ?).
          </p>
        )}
      </header>

      {sections.map((section) => (
        <section className="list-section" key={section.id}>
          <h4>
            <span>{section.label}</span>
            <span className="muted">
              {section.items.filter((i) => i.ticked).length}/{section.items.length}
            </span>
          </h4>
          <ul className="list-items">
            {section.items.map((item) => (
              <li key={item.id} className={`list-item${item.ticked ? " got" : ""}`}>
                <button
                  type="button"
                  className="list-tick"
                  role="checkbox"
                  aria-checked={item.ticked}
                  onClick={() => onTick(item)}
                >
                  <span className="box" aria-hidden="true">
                    {item.ticked && <CheckIcon size={14} />}
                  </span>
                  <span className="list-text">
                    <span className="list-name">
                      {item.status === "substitute" && item.substitute ? (
                        <>
                          {cap(item.substitute)} <span className="muted small">instead of {item.name}</span>
                        </>
                      ) : (
                        cap(item.name)
                      )}
                    </span>
                    {quantity(item) && <span className="list-qty">{quantity(item)}</span>}
                    {choices?.[item.name] ? (
                      <span className="list-pick">
                        {choices[item.name].name}
                        <span className="muted">
                          {" "}
                          · {choices[item.name].store}
                          {choices[item.name].aisle ? ` · ${aisleText(choices[item.name].aisle)}` : ""}
                        </span>
                      </span>
                    ) : item.brand ? (
                      <span className="list-pick muted">{item.brand}</span>
                    ) : null}
                  </span>
                  <StatusMark status={item.status} />
                </button>
                <button
                  type="button"
                  className={`icon-btn list-more${open === item.id ? " open" : ""}`}
                  aria-expanded={open === item.id}
                  aria-label={`Products for ${searchedName(item)}`}
                  onClick={() => setOpen(open === item.id ? null : item.id)}
                >
                  <ChevronIcon size={16} />
                </button>
                {open === item.id && (
                  <div className="list-detail">
                    <p className="small">{statusText(item)}</p>
                    {result ? (
                      products(item).length ? (
                        <ul className="list-products">
                          {products(item).map(({ product, store }) => (
                            <li key={`${product.store_id}:${product.name}`}>
                              <VerdictBadge verdict={product.verdict} />
                              <span>
                                <strong>{product.name}</strong>
                                <span className="muted small">
                                  {" "}
                                  · {store?.name ?? "store"}
                                  {store ? ` (${milesText(store.distance_miles)})` : ""}
                                  {product.aisle ? ` · ${aisleText(product.aisle)}` : ""}
                                </span>
                                <span className="muted small list-reason">{product.reason}</span>
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="muted small">No products found for this near {list.zip}.</p>
                      )
                    ) : (
                      <p className="muted small">Find products again to see what's on the shelves near {list.zip}.</p>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}

      {skip.length > 0 && (
        <section className="list-section skip">
          <h4>
            <span>Leave out</span>
          </h4>
          <ul className="list-items plain">
            {skip.map((item) => (
              <li key={item.id} className="list-item">
                <span className="list-name">{cap(item.name)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="list-buttons">
        {list.steps.length > 0 && (
          <button className={`btn ghost${panel === "steps" ? " on" : ""}`} onClick={() => togglePanel("steps")}>
            Steps ({list.steps.length})
          </button>
        )}
        {result && (
          <>
            <button className={`btn ghost${panel === "stores" ? " on" : ""}`} onClick={() => togglePanel("stores")}>
              Stores ({result.stores.length})
            </button>
            <button
              className={`btn ghost${panel === "products" ? " on" : ""}`}
              onClick={() => togglePanel("products")}
            >
              All products
            </button>
          </>
        )}
        {actions}
      </div>

      {panel === "steps" && (
        <div className="list-panel">
          {summary && <p className="summary">{summary}</p>}
          <ol className="steps-list">
            {list.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
      )}
      {panel === "stores" && result && (
        <div className="list-panel">
          <Stores result={result} answer={answer ?? null} mode={mode} />
        </div>
      )}
      {panel === "products" && result && (
        <div className="list-panel">
          <Products result={result} storeById={storeById} />
        </div>
      )}
    </div>
  );
}

function StatusMark({ status }: { status: SavedItem["status"] }) {
  if (status === "buy")
    return (
      <span className="mark safe" title="A safe product was found">
        <CheckIcon size={14} />
      </span>
    );
  if (status === "substitute")
    return (
      <span className="mark swap" title="Use the substitute">
        <RefreshIcon size={14} />
      </span>
    );
  return (
    <span className="mark unknown" title="Read the label before buying">
      <QuestionIcon size={14} />
    </span>
  );
}

function statusText(item: SavedItem): string {
  if (item.status === "buy") return "A product marked safe for you was found.";
  if (item.status === "substitute")
    return `No safe ${item.name} was found, so the list uses ${item.substitute ?? "a substitute"}. It's a suggestion: check its label too.`;
  return "No product could be fully checked: read the label before buying.";
}

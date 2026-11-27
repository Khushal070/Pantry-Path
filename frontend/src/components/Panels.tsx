import type { Answer, Result, Store, Verdict } from "../types";
import { EVIDENCE_LABEL, VERDICT_LABEL, cap, milesText, routeLink, storeLink } from "../format";
import { CheckIcon, QuestionIcon, RouteIcon, XIcon } from "../icons";

/** The store and product views behind the list's buttons (ListView). Store data is live
 * only: it is never saved, so these appear only while a search's results are in memory. */

export const VERDICT_ORDER: Record<Verdict, number> = { safe: 0, unknown: 1, unsafe: 2 };

export function Stores({ result, answer, mode }: { result: Result; answer: Answer | null; mode: "demo" | "live" }) {
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

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
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

export function Products({ result, storeById }: { result: Result; storeById: Map<string, Store> }) {
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

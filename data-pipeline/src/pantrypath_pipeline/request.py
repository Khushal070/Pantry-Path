"""Request flow: dish + ZIP + allergies + diet -> result.json (contract v1.0).

    python -m pantrypath_pipeline.request "quiche" --zip 45202 --avoid milk gluten --diet vegetarian

1. Dish: the SQLite dish catalog first (exact, alias, then fuzzy match); the LLM
   only for dishes the catalog doesn't have (a paid call, needs LLM_API_KEY).
2. Stores: the first provider in request.providers (Kroger) with stores in range.
3. Products: every ingredient x store, searched in parallel.
4. Evidence: the store's own allergen data, else the Open Food Facts snapshot by
   barcode, else none (the verdict is then "unknown", never "safe").
5. Verdicts from pantrypath_core.diet_rules, one per product.
6. Later providers (Target) only fill gaps: no store in range, or no in-stock
   product for an ingredient. request.parallel_providers asks all of them at once.

result.json is temporary by design (meta.expires_at): Kroger's terms forbid keeping
copies of their data, so results/ is gitignored and nothing else is stored.
"""

import argparse
import logging
import re
import sqlite3
import string
import sys
import time
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pantrypath_core import diet_rules
from pantrypath_core.catalog import DishProfile, slugify
from pantrypath_core.diet_rules import Allergen, Diet, ProductEvidence
from pantrypath_core.result import (
    DishInfo,
    IngredientResult,
    Meta,
    Product,
    RequestInfo,
    Result,
    Store,
    Summary,
)
from pantrypath_pipeline import off_lookup
from pantrypath_pipeline.config import Config, load_config, secret
from pantrypath_pipeline.errors import DataCorruptError, PipelineError, ProviderError
from pantrypath_pipeline.logging_setup import setup_logging
from pantrypath_pipeline.providers.base import ProviderProduct, StoreProvider, make_providers
from pantrypath_pipeline.store import find_dish

log = logging.getLogger(__name__)

DishSource = Literal["catalog", "llm"]
SearchPlan = tuple[StoreProvider, list[Store], list[str]]  # provider, its stores, search terms


# Hooks to the other pipeline pieces, kept as module functions so tests can replace them.

def catalog_lookup(db_path: Path, query: str, min_score: float) -> DishProfile | None:
    try:
        return find_dish(db_path, query, min_score)
    except sqlite3.DatabaseError as exc:
        raise DataCorruptError(
            f"dish catalog {db_path} can't be read ({exc}); rebuild it or `dvc pull` it"
        ) from exc


LLMTemplate = tuple[string.Template, str]  # (prompt text, prompt version)


def llm_profile(cfg: Config, query: str, client: Any,
                template: LLMTemplate | None = None) -> DishProfile:
    """A profile from the LLM (source 'llm'); client None = the one configured in .env,
    template None = prompts/refine_dish.txt."""
    from pantrypath_pipeline import refine  # imported on use: the LLM stack is heavy

    return refine.profile_for_unknown_dish(cfg, query, client, template=template)


def off_evidence(cfg: Config, barcodes: Iterable[str]) -> dict[str, ProductEvidence]:
    """Open Food Facts evidence by GTIN-13; empty, with a warning, if the snapshot is missing."""
    barcodes = sorted(set(barcodes))
    if not barcodes:
        return {}
    try:
        return off_lookup.lookup(cfg, barcodes)
    except PipelineError as exc:
        log.warning("%s; verdicts use store data only", exc)
        return {}


def resolve_dish(cfg: Config, query: str, llm_client: Any = None,
                 llm_template: LLMTemplate | None = None) -> tuple[DishProfile, DishSource]:
    """The catalog's profile for the dish, else one from the LLM (with llm_template as its
    prompt, if given)."""
    db_path = cfg.path(cfg["catalog"]["sqlite_output"])
    if db_path.is_file():
        profile = catalog_lookup(db_path, query, cfg["request"]["fuzzy_min_score"])
        if profile is not None:
            log.info("Dish %r: catalog match %r", query, profile.name)
            return profile, "catalog"
        reason = "is not in the dish catalog"
    else:
        reason = f"can't be looked up (no dish catalog at {db_path})"
    if llm_client is None and not secret("LLM_API_KEY"):
        raise PipelineError(
            f"dish {query!r} {reason}, and no LLM key is set for the fallback: build the "
            "catalog (batch pipeline or `dvc pull`) or set LLM_API_KEY in .env"
        )
    log.info("Dish %r %s; asking the LLM (paid call)", query, reason)
    return llm_profile(cfg, query, llm_client, llm_template), "llm"


def search_plans(
    pool: ThreadPoolExecutor, plans: list[SearchPlan], limit: int, errors: list[str]
) -> dict[tuple[str, str], list[ProviderProduct]]:
    """Run every term x store search of every plan in parallel.

    Returns products keyed by (term, store_id). A failed search is logged, added to
    `errors` and left out, so one bad call doesn't sink the whole request.
    """
    futures = {
        pool.submit(provider.search_products, term, store, limit): (provider.name, term, store)
        for provider, stores, terms in plans
        for term in terms
        for store in stores
    }
    results: dict[tuple[str, str], list[ProviderProduct]] = {}
    for future in as_completed(futures):
        name, term, store = futures[future]
        try:
            results[(term, store.store_id)] = future.result()
        except ProviderError as exc:
            errors.append(f"{name} search {term!r} at {store.store_id}: {exc}")
            log.warning("%s search %r at %s failed: %s", name, term, store.store_id, exc)
    return results


def has_stock(products: list[ProviderProduct]) -> bool:
    """True when at least one product is in stock or its stock is unknown."""
    return any(p.in_stock is not False for p in products)


def gather_products(
    cfg: Config, providers: list[StoreProvider], terms: list[str], zip_code: str
) -> tuple[dict[str, list[ProviderProduct]], list[Store], list[str]]:
    """Products per search term (store order, then relevance), their stores, providers used."""
    settings = cfg["request"]
    radius, limit = settings["radius_miles"], settings["max_products"]
    found: dict[str, list[ProviderProduct]] = {term: [] for term in terms}
    stores: list[Store] = []
    used: list[str] = []
    errors: list[str] = []

    def stores_of(provider: StoreProvider) -> list[Store]:
        try:
            return provider.find_stores(zip_code, radius, settings["max_stores"])
        except ProviderError as exc:
            errors.append(f"{provider.name} stores: {exc}")
            log.warning("%s store lookup failed: %s", provider.name, exc)
            return []

    def merge(plans: list[SearchPlan], results: dict[tuple[str, str], list[ProviderProduct]]):
        for provider, provider_stores, provider_terms in plans:
            if not any((t, s.store_id) in results for t in provider_terms for s in provider_stores):
                continue  # every search failed: this provider adds nothing
            for term in provider_terms:
                for store in provider_stores:
                    found[term].extend(results.get((term, store.store_id), []))
            stores.extend(s for s in provider_stores if s not in stores)
            used.append(provider.name)

    with ThreadPoolExecutor(max_workers=settings["max_workers"]) as pool:
        if settings.get("parallel_providers"):
            plans = [(p, s, list(terms)) for p in providers if (s := stores_of(p))]
            merge(plans, search_plans(pool, plans, limit, errors))
        else:
            missing = list(terms)
            for provider in providers:
                if not missing:
                    break
                provider_stores = stores_of(provider)
                if not provider_stores:
                    log.info("%s: no store within %s miles of %s", provider.name, radius, zip_code)
                    continue
                plans = [(provider, provider_stores, missing)]
                merge(plans, search_plans(pool, plans, limit, errors))
                missing = [term for term in missing if not has_stock(found[term])]
                if missing:
                    log.info("%s: no in-stock product for %s", provider.name, ", ".join(missing))

    if errors and not any(found.values()):
        raise ProviderError("no store data could be fetched: " + " | ".join(errors[:3]))
    if not stores:
        log.warning("No store within %s miles of %s from %s", radius, zip_code,
                    ", ".join(p.name for p in providers))
    return found, stores, used


def judge(
    product: ProviderProduct, off: dict[str, ProductEvidence], request: RequestInfo
) -> Product:
    """One product's verdict: store evidence first, then Open Food Facts, else unknown."""
    off_match = off.get(product.barcode) if product.barcode else None
    evidence = diet_rules.pick_evidence(product.evidence, off_match)
    verdict = diet_rules.assess(evidence, request.avoid, request.diet)
    return Product(
        store_id=product.store_id,
        barcode=product.barcode,
        name=product.name,
        brand=product.brand,
        size=product.size,
        in_stock=product.in_stock,
        aisle=product.aisle,
        verdict=verdict.verdict,
        reason=verdict.reason,
        evidence_source=verdict.evidence_source,
    )


def build_result(
    cfg: Config,
    query: str,
    zip_code: str,
    avoid: Iterable[Allergen | str],
    diet: Diet | str,
    *,
    providers: list[StoreProvider] | None = None,
    llm_client: Any = None,
    llm_template: LLMTemplate | None = None,
) -> Result:
    """Run the whole request flow and return the validated result. llm_client and
    llm_template (prompt text, version) replace the LLM fallback's client and prompt for a
    dish the catalog doesn't have; by default they are the .env client and
    prompts/refine_dish.txt."""
    request = RequestInfo(query=query, zip=zip_code, avoid=list(avoid), diet=diet)
    profile, source = resolve_dish(cfg, request.query, llm_client, llm_template)
    providers = providers if providers is not None else make_providers(cfg)
    terms = [ingredient.name for ingredient in profile.ingredients]
    found, stores, used = gather_products(cfg, providers, terms, request.zip)
    off = off_evidence(cfg, (p.barcode for products in found.values() for p in products
                             if p.barcode))
    ingredients = [
        IngredientResult(
            name=ingredient.name,
            role=ingredient.role,
            category=ingredient.category,
            products=[judge(p, off, request) for p in found[ingredient.name]],
        )
        for ingredient in profile.ingredients
    ]
    now = datetime.now(UTC)
    return Result(
        request=request,
        dish=DishInfo(name=profile.name, cuisine=profile.cuisine, source=source),
        stores=stores,
        ingredients=ingredients,
        summary=Summary.from_ingredients(ingredients),
        meta=Meta(
            generated_at=now,
            expires_at=now + timedelta(hours=cfg["request"]["result_ttl_hours"]),
            providers_used=used,
            catalog_version=profile.catalog_version if source == "catalog" else "llm",
        ),
    )


def default_output_path(cfg: Config, result: Result) -> Path:
    stamp = result.meta.generated_at.strftime("%Y%m%dT%H%M%SZ")
    return cfg.path(cfg["paths"]["results"]) / f"{stamp}_{slugify(result.dish.name)}.json"


def describe(result: Result, out: Path, elapsed_s: float, calls: dict[str, int]) -> str:
    """A short human summary of a result for the terminal."""
    summary = result.summary
    verdicts = Counter(p.verdict.value for i in result.ingredients for p in i.products)
    stores = "; ".join(f"{s.name} ({s.distance_miles:.1f} mi)" for s in result.stores)
    lines = [
        f"{result.dish.name} ({result.dish.cuisine.value}, source: {result.dish.source}) "
        f"near {result.request.zip}",
        f"stores: {stores or 'none in range'}",
        f"ingredients with a safe product: {summary.with_safe_product}/{summary.ingredients_total}",
    ]
    if summary.without_safe_product:
        lines.append("no safe product found for: " + ", ".join(summary.without_safe_product))
    counts = ", ".join(f"{verdicts[kind]} {kind}" for kind in ("safe", "unsafe", "unknown"))
    lines.append(f"verdicts: {counts}")
    lines.append(
        "provider calls: " + ", ".join(f"{name} {n}" for name, n in calls.items())
        + f" | {elapsed_s:.1f} s"
    )
    lines.append(f"wrote {out} (expires {result.meta.expires_at:%Y-%m-%d %H:%M} UTC)")
    return "\n".join(lines)


def _zip_code(value: str) -> str:
    if not re.fullmatch(r"\d{5}", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a 5-digit US ZIP code")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pantrypath_pipeline.request",
        description="Find allergen-checked products for a dish near a ZIP; write result.json.",
    )
    parser.add_argument("query", help='the dish, e.g. "quiche"')
    parser.add_argument("--zip", dest="zip_code", required=True, type=_zip_code,
                        help="5-digit US ZIP code")
    parser.add_argument("--avoid", nargs="*", default=[], metavar="ALLERGEN",
                        choices=[a.value for a in Allergen],
                        help="allergens to avoid: " + ", ".join(a.value for a in Allergen))
    parser.add_argument("--diet", default=Diet.none.value, choices=[d.value for d in Diet])
    parser.add_argument("--out", type=Path,
                        help="output file (default: results/<UTC time>_<dish>.json)")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.path(cfg["paths"]["logs"]))
    started = time.monotonic()
    providers: list[StoreProvider] = []
    try:
        providers = make_providers(cfg)
        result = build_result(cfg, args.query, args.zip_code, args.avoid, args.diet,
                              providers=providers)
    except PipelineError as exc:
        log.error("Request failed: %s", exc)
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    out = args.out or default_output_path(cfg, result)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.model_dump_json(indent=2) + "\n")
    calls = {p.name: p.calls for p in providers}
    print(describe(result, out, time.monotonic() - started, calls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

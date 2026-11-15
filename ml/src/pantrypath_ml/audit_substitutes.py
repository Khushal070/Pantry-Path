"""Audit configs/substitutes.yaml `contains` lists against the Open Food Facts snapshot.

For each substitute, the products in the snapshot that are that substitute (by name), and
the allergens they declare (allergens_tags, plus wheat from the ingredient text: OFF has
no wheat tag), their traces (traces_tags, "may contain") and their vegetarian / vegan
analysis. A substitute is flagged when an allergen missing from its contains list is
declared by at least --declared-min of its products (or is a trace in at least
--trace-min), or when a diet tag is missing likewise. Substitutes with fewer than
--min-products matches are listed as not auditable (home preparations such as flax egg,
or names OFF doesn't use).

A product matches when its name (sizes and a trailing parenthesis dropped) ends with the
substitute's name, plurals and hyphens allowed, followed at most by a few descriptive
words ("Organic Coconut Milk Unsweetened"), and has at most --max-words words: the product
is the substitute, not something made with it ("Coconut Milk Chocolate"). Read each flag's
examples before changing the table: products made with the substitute still slip in.

Open Food Facts only (no Kroger data). Read-only on the table; not run in CI (it needs the
snapshot: `dvc pull data-pipeline/data/processed/off_products.parquet.dvc`).

    python -m pantrypath_ml.audit_substitutes [--off PATH] [--declared-min 0.10] \\
        [--trace-min 0.20] [--min-products 5] [--max-words 7]

Writes reports/audit/<id>/audit.json and audit.md.
"""

import argparse
import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from pantrypath_ml import config
from pantrypath_ml.reporting import write_json
from pantrypath_pipeline.errors import DataUnavailableError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

TAGS = {"en:milk": "milk", "en:eggs": "egg", "en:soybeans": "soy", "en:nuts": "tree_nuts",
        "en:peanuts": "peanuts", "en:sesame-seeds": "sesame", "en:fish": "fish",
        "en:crustaceans": "shellfish", "en:molluscs": "shellfish", "en:gluten": "gluten"}
WHEAT = re.compile(r"(?<!buck)\bwheat\b", re.I)
ORDER = ["milk", "egg", "fish", "shellfish", "tree_nuts", "peanuts", "wheat", "soy", "sesame",
         "gluten"]
NOT_VEGAN = {"meat", "animal", "milk", "egg", "fish", "shellfish"}
TAIL = (r"(?:\s+(?:original|unsweetened|sweetened|plain|organic|natural|beverage|drink|"
        r"classic|light|lite|raw|whole|dry|dried|uncooked|style|blend|vanilla|"
        r"non[\s-]?dairy|dairy[\s-]?free|gluten[\s-]?free|vegan|plant[\s-]?based))*")
SIZE = re.compile(r"[\s,(]*\d[\d.,/]*\s*(?:fl\.?\s*oz|oz|lbs?|g|kg|ml|l|ct|count|pack|pk)\b"
                  r"\.?\)?\s*$|\s*\([^()]*\)\s*$", re.I)


def base_name(name: str) -> str:
    """The product to look for: 'soy milk with lemon juice' -> 'soy milk'."""
    name = re.sub(r"\s*\(.*?\)", "", name.lower())
    return re.split(r"\s+with\s+", name)[0].strip()


def phrase_regex(name: str) -> str:
    words = re.split(r"[\s-]+", base_name(name))
    phrase = r"[\s-]+".join(re.escape(w) + r"(?:s|es)?" for w in words)
    return rf"\b{phrase}{TAIL}[\s.!]*$"


def clean_name(product_name: str | None) -> str | None:
    if product_name is None:
        return None
    name = " ".join(product_name.lower().split())
    for _ in range(3):
        name = SIZE.sub("", name).strip()
    return name


def substitutes(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each substitute once, with its keys and any key where its contains list differs."""
    out: dict[str, dict[str, Any]] = {}
    for key, cands in table["substitutes"].items():
        for c in cands:
            e = out.setdefault(c["name"], {"contains": c["contains"], "keys": []})
            e["keys"].append(key)
            if set(c["contains"]) != set(e["contains"]):
                e.setdefault("inconsistent", []).append({"key": key, "contains": c["contains"]})
    return out


def score(rows: list[tuple], contains: set[str], *, declared_min: float, trace_min: float,
          min_products: int) -> dict[str, Any]:
    """Flags for one substitute from its matching products' (name, allergens_tags,
    traces_tags, ingredients_text, ingredients_analysis_tags) rows."""
    n = len(rows)
    declared, traces, diet = Counter(), Counter(), Counter()
    examples: dict[str, list[str]] = {}
    for pname, atags, ttags, text, analysis in rows:
        have = {TAGS[t] for t in atags or [] if t in TAGS}
        if WHEAT.search(text or ""):
            have.add("wheat")
        for a in have:
            declared[a] += 1
            if len(examples.setdefault(a, [])) < 4:
                examples[a].append(pname)
        for a in {TAGS[t] for t in ttags or [] if t in TAGS} - have:
            traces[a] += 1
        for tag in analysis or []:
            if tag in ("en:non-vegetarian", "en:non-vegan"):
                diet[tag[3:]] += 1
    flags: list[dict[str, Any]] = []
    if n >= min_products:
        for a in ORDER:
            if a not in contains and declared[a] / n >= declared_min:
                flags.append({"allergen": a, "kind": "declared",
                              "share": round(declared[a] / n, 3), "products": declared[a],
                              "examples": examples.get(a, [])})
        flagged = {f["allergen"] for f in flags}
        for a in ORDER:
            if a not in contains and a not in flagged and traces[a] / n >= trace_min:
                flags.append({"allergen": a, "kind": "trace", "share": round(traces[a] / n, 3),
                              "products": traces[a]})
        if "meat" not in contains and diet["non-vegetarian"] / n >= declared_min:
            flags.append({"allergen": "meat", "kind": "diet",
                          "share": round(diet["non-vegetarian"] / n, 3)})
        if not contains & NOT_VEGAN and diet["non-vegan"] / n >= declared_min:
            flags.append({"allergen": "animal", "kind": "diet",
                          "share": round(diet["non-vegan"] / n, 3)})
    return {"matched": n, "auditable": n >= min_products, "flags": flags,
            "declared": {a: round(declared[a] / n, 3) for a in ORDER if n and declared[a]}}


def run(off: Path, table_path: Path, *, declared_min: float = 0.10, trace_min: float = 0.20,
        min_products: int = 5, max_words: int = 7) -> dict[str, Any]:
    import duckdb

    if not off.is_file():
        raise DataUnavailableError(f"{off} not found: dvc pull "
                                   "data-pipeline/data/processed/off_products.parquet.dvc")
    con = duckdb.connect()
    con.create_function("clean_name", clean_name, ["VARCHAR"], "VARCHAR")
    con.execute("CREATE TABLE off AS SELECT clean_name(product_name) AS name, product_name, "
                "allergens_tags, traces_tags, ingredients_text, ingredients_analysis_tags "
                "FROM read_parquet(?) WHERE product_name IS NOT NULL AND ingredients_text IS "
                "NOT NULL AND length(ingredients_text) > 3", [str(off)])
    total = con.execute("SELECT count(*) FROM off").fetchall()[0][0]
    results = []
    for name, e in sorted(substitutes(yaml.safe_load(table_path.read_text())).items()):
        rows = con.execute(
            "SELECT product_name, allergens_tags, traces_tags, ingredients_text, "
            "ingredients_analysis_tags FROM off WHERE regexp_matches(name, ?) "
            "AND len(string_split(name, ' ')) <= ?", [phrase_regex(name), max_words]).fetchall()
        results.append({"name": name, "keys": e["keys"], "contains": sorted(e["contains"]),
                        "inconsistent": e.get("inconsistent"),
                        **score(rows, set(e["contains"]), declared_min=declared_min,
                                trace_min=trace_min, min_products=min_products)})
    return {"products_with_ingredients": total, "table": str(table_path), "off": str(off),
            "rules": {"declared_min": declared_min, "trace_min": trace_min,
                      "min_products": min_products, "max_words": max_words},
            "results": results}


def render(audit: dict[str, Any]) -> str:
    lines = ["# Substitutes audit against Open Food Facts", "",
             f"{audit['products_with_ingredients']} products with an ingredient list; rules "
             f"`{json.dumps(audit['rules'])}`. Read each flag's examples: products made with "
             "the substitute can still match.", "", "## Flags", ""]
    for r in audit["results"]:
        for f in r["flags"]:
            ex = "; ".join(f.get("examples") or [])
            lines.append(f"- **{r['name']}** (contains {r['contains']}, {r['matched']} "
                         f"products): {f['allergen']} {f['kind']} {f['share']:.0%}"
                         + (f". E.g. {ex}" if ex else ""))
    ok = [r["name"] for r in audit["results"] if r["auditable"] and not r["flags"]]
    low = [f"{r['name']} ({r['matched']})" for r in audit["results"] if not r["auditable"]]
    lines += ["", f"## No flag ({len(ok)})", "", ", ".join(ok) or "none", "",
              f"## Not auditable ({len(low)})", "", ", ".join(low) or "none", ""]
    bad = [r for r in audit["results"] if r["inconsistent"]]
    if bad:
        lines += ["## Inconsistent contains lists", ""]
        lines += [f"- {r['name']}: {r['inconsistent']}" for r in bad]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.audit_substitutes",
                                     description=__doc__.splitlines()[0])
    pcfg = config.pipeline_config()
    parser.add_argument("--off", type=Path, default=pcfg.path(pcfg["off"]["output"]))
    parser.add_argument("--table", type=Path,
                        default=config.ML_HOME / "configs" / "substitutes.yaml")
    parser.add_argument("--declared-min", type=float, default=0.10)
    parser.add_argument("--trace-min", type=float, default=0.20)
    parser.add_argument("--min-products", type=int, default=5)
    parser.add_argument("--max-words", type=int, default=7)
    args = parser.parse_args(argv)
    setup_logging()
    try:
        audit = run(args.off, args.table, declared_min=args.declared_min,
                    trace_min=args.trace_min, min_products=args.min_products,
                    max_words=args.max_words)
    except DataUnavailableError as exc:
        log.error("%s", exc)
        return 1
    folder = config.REPORTS / "audit" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-substitutes"
    write_json(folder / "audit.json", audit)
    (folder / "audit.md").write_text(render(audit))
    flagged = [r["name"] for r in audit["results"] if r["flags"]]
    print(json.dumps({"folder": str(folder), "flagged": flagged,
                      "not_auditable": sum(not r["auditable"] for r in audit["results"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

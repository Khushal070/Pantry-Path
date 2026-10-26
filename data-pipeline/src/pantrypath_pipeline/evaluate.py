"""Catalog evaluation: does each dish profile list what a real recipe needs?

Scores the dish catalog against hand-written reference recipes (data/golden/) in two
splits:
- dev: the first 16 golden recipes, which we looked at while building the refine
  prompt and the always_include list, so their scores are optimistic;
- test (held out): recipes written after the catalog was frozen (2026-10-26), for
  catalog dishes nobody compared by hand. The catalog is replayed from saved LLM
  replies, so these recipes can't have influenced it.

Per split:
- recall: share of the reference's required ingredients (not optional, not water)
  found anywhere in the dish profile. A miss means a shopper could leave out
  something the dish needs.
- recall without LLM additions: the same, counting only ingredients that came from
  Food.com. The gap to recall is what the LLM pass added on unseen dishes.
- core precision: share of the profile's core ingredients that the reference uses
  (optional ones included). Low means the profile asks for things a cook may not use.

Names match when they share the last word and differ only by words in MODIFIERS
("all-purpose flour" = "flour", "boneless chicken breast" = "chicken breast"), after
the synonym pairs in config. The matcher was tuned on the dev split only.
Test recall below catalog_eval.min_recall_warn alerts; the step never fails the run.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError

log = logging.getLogger(__name__)

OK, WARNING = "ok", "warning"

# Words that don't change what you buy: size, grade, cut and preparation
MODIFIERS = frozenset({
    "all", "purpose", "baby", "beaten", "bittersweet", "boiled", "boneless", "bought",
    "canned", "chopped",
    "cold", "cooked", "crushed", "diced", "dried", "dry", "extra", "fine", "finely", "fresh",
    "frozen", "grated", "granulated", "ground", "hard", "kosher", "large", "lean", "light",
    "long", "grain", "low", "medium", "melted", "minced", "mild", "organic", "packed",
    "plain", "prepared", "pure", "raw", "reduced", "refrigerated", "ripe", "salted", "sea",
    "semisweet", "sharp", "shredded", "skinless", "sliced", "small", "sodium", "softened",
    "store",
    "table", "uncooked", "unbaked", "unsalted", "virgin", "warm", "white", "whole",
})


def singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(name: str) -> list[str]:
    return [singular(w) for w in re.findall(r"[a-z0-9]+", name.lower())]


class Matcher:
    """Decides whether a catalog ingredient name stands for a reference ingredient."""

    def __init__(self, synonyms: list[list[str]] | None = None):
        # every name in a synonym group maps to the group's first name
        self.canonical: dict[str, str] = {}
        for group in synonyms or []:
            for name in group:
                self.canonical[" ".join(tokens(name))] = " ".join(tokens(group[0]))

    def key(self, name: str) -> list[str]:
        joined = " ".join(tokens(name))
        return self.canonical.get(joined, joined).split()

    def same(self, a: str, b: str) -> bool:
        ta, tb = self.key(a), self.key(b)
        if not ta or not tb:
            return False
        if ta == tb:
            return True
        return ta[-1] == tb[-1] and set(ta) ^ set(tb) <= MODIFIERS


def read_recipes(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DataUnavailableError(f"Reference recipes {path} not found")
    return json.loads(path.read_text())


def find_dish(dish: str, catalog: list[dict[str, Any]],
              overrides: dict[str, str]) -> dict[str, Any] | None:
    """The catalog dish for a reference recipe: config override, then name or alias."""
    wanted = overrides.get(dish.lower())
    by_id = {d["dish_id"]: d for d in catalog}
    if wanted:
        return by_id.get(wanted)
    target = " ".join(tokens(dish))
    for d in catalog:
        names = [d["name"], d["dish_id"].replace("-", " "), *d.get("aliases", [])]
        if any(" ".join(tokens(n)) == target for n in names):
            return d
    return None


def score_dish(recipe: dict[str, Any], profile: dict[str, Any], matcher: Matcher,
               skip: set[str]) -> dict[str, Any]:
    reference = [i for i in recipe["ingredients"] if i["name"].lower() not in skip]
    required = [i["name"] for i in reference if not i.get("optional")]
    catalog = profile["ingredients"]
    from_source = [c["name"] for c in catalog if not c.get("added_by_llm")]
    core = [c["name"] for c in catalog if c.get("role") == "core"]

    def found(name: str, pool: list[str]) -> bool:
        return any(matcher.same(name, other) for other in pool)

    missed = [n for n in required if not found(n, [c["name"] for c in catalog])]
    missed_source = [n for n in required if not found(n, from_source)]
    extra_core = [n for n in core if not found(n, [i["name"] for i in reference])]
    return {
        "dish": recipe["dish"],
        "dish_id": profile["dish_id"],
        "cuisine": profile.get("cuisine"),
        "required": len(required),
        "found": len(required) - len(missed),
        "found_without_llm": len(required) - len(missed_source),
        "core": len(core),
        "core_in_reference": len(core) - len(extra_core),
        "missed": missed,
        "missed_without_llm": missed_source,
        "core_not_in_reference": extra_core,
    }


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def score_split(recipes: list[dict[str, Any]], catalog: list[dict[str, Any]],
                matcher: Matcher, settings: dict[str, Any]) -> dict[str, Any]:
    overrides = {k.lower(): v for k, v in (settings.get("dish_ids") or {}).items()}
    skip = {s.lower() for s in settings.get("skip_ingredients") or []}
    dishes, missing = [], []
    for recipe in recipes:
        profile = find_dish(recipe["dish"], catalog, overrides)
        if profile is None:
            missing.append(recipe["dish"])
        else:
            dishes.append(score_dish(recipe, profile, matcher, skip))
    total = {k: sum(d[k] for d in dishes)
             for k in ("required", "found", "found_without_llm", "core", "core_in_reference")}
    return {
        "recipes": len(recipes),
        "dishes_scored": len(dishes),
        "dishes_not_in_catalog": missing,
        "recall": _ratio(total["found"], total["required"]),
        "recall_without_llm": _ratio(total["found_without_llm"], total["required"]),
        "core_precision": _ratio(total["core_in_reference"], total["core"]),
        "dishes_fully_covered": sum(d["found"] == d["required"] for d in dishes),
        "totals": total,
        "dishes": dishes,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Catalog evaluation: dish profiles against reference recipes", "",
        f"_Generated {report['generated_at']} by `python -m pantrypath_pipeline "
        "eval-catalog`._", "",
        "Each catalog dish lists the ingredients a shopper needs. We compare that list "
        "with a hand-written reference recipe for the same dish. **dev** = the 16 golden "
        "recipes we used while building the pipeline (optimistic). **test** = recipes "
        "written after the catalog was frozen, for dishes nobody checked by hand (held "
        "out).", "",
        "| Split | Dishes | Recall | Recall without LLM additions | Core precision "
        "| Dishes fully covered |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in report["splits"].items():
        lines.append(f"| {name} | {s['dishes_scored']} | {_pct(s['recall'])} "
                     f"| {_pct(s['recall_without_llm'])} | {_pct(s['core_precision'])} "
                     f"| {s['dishes_fully_covered']} of {s['dishes_scored']} |")
    lines += [
        "", "- *Recall*: share of the reference's required ingredients found in the dish "
        "profile.",
        "- *Recall without LLM additions*: the same, counting only ingredients that came "
        "from Food.com.",
        "- *Core precision*: share of the profile's core ingredients that the reference "
        "recipe uses.", "",
    ]
    for name, s in report["splits"].items():
        if s["dishes_not_in_catalog"]:
            lines += [f"Not in the catalog ({name}): {', '.join(s['dishes_not_in_catalog'])}", ""]
        lines += [f"## {name}: per dish", "",
                  "| Dish | Found | Missed | Core not in the reference |", "|---|---|---|---|"]
        for d in s["dishes"]:
            lines.append(f"| {d['dish']} | {d['found']}/{d['required']} "
                         f"| {', '.join(d['missed']) or '–'} "
                         f"| {', '.join(d['core_not_in_reference']) or '–'} |")
        lines.append("")
    lines += [
        "## Limits", "",
        "- One reference recipe per dish; real recipes vary, so a \"miss\" can be a "
        "legitimate variant, and core precision is a lower bound.",
        "- Name matching is rule-based (shared last word, differing only by size or "
        "preparation words, plus the synonym pairs in config). It was tuned on the dev "
        "split only. Held-out misses that are only naming differences (\"breadcrumbs\" vs "
        "\"bread crumbs\") are left as misses, so held-out recall is a lower bound.",
        "- The reference recipes were drafted with an AI assistant and need a check by a "
        "teammate.",
    ]
    return "\n".join(lines) + "\n"


def run_catalog(cfg: Config, **_: Any) -> dict[str, Any]:
    """Step eval-catalog: score the dish profiles on the dev and held-out test recipes."""
    settings = cfg["catalog_eval"]
    profiles_path = cfg.path(cfg["catalog"]["profiles_output"])
    if not profiles_path.is_file():
        raise DataUnavailableError(f"{profiles_path} not found; run assemble-catalog first")
    catalog = json.loads(profiles_path.read_text())["dishes"]
    matcher = Matcher(settings.get("synonyms"))

    splits = {name: score_split(read_recipes(cfg.path(path)), catalog, matcher, settings)
              for name, path in settings["splits"].items()}
    test = splits.get("test") or {}
    floor = float(settings["min_recall_warn"])
    problems = []
    if test.get("recall") is not None and test["recall"] < floor:
        problems.append(f"held-out recall {_pct(test['recall'])} is below {_pct(floor)}")
    for name, s in splits.items():
        if s["dishes_not_in_catalog"]:
            problems.append(f"{name}: not in the catalog: {', '.join(s['dishes_not_in_catalog'])}")

    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "status": WARNING if problems else OK,
        "min_recall_warn": floor,
        "problems": problems,
        "splits": splits,
    }
    json_path, md_path = cfg.path(settings["output_json"]), cfg.path(settings["output_md"])
    for path in (json_path, md_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    md_path.write_text(render_markdown(report))
    log.info("eval-catalog: %s, wrote %s and %s", report["status"], json_path, md_path)
    if problems:
        send_alert("catalog evaluation: check the dish profiles", "\n".join(problems),
                   level="warning", cfg=cfg)
    return {"status": report["status"],
            **{f"{name}_recall": s["recall"] for name, s in splits.items()},
            "report": str(md_path)}

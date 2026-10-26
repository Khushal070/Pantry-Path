"""The hand-written golden recipes must all match the recipe schema.

recipes.json is the dev split (looked at while building the pipeline);
recipes_heldout.json is the held-out test split. A dish may only be in one of them.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from pantrypath_core.schema import Recipe

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "data" / "golden"
SPLITS = {name: json.loads((GOLDEN_DIR / file).read_text())
          for name, file in (("dev", "recipes.json"), ("test", "recipes_heldout.json"))}
RECORDS = [r for records in SPLITS.values() for r in records]


@pytest.mark.parametrize("record", RECORDS, ids=[r.get("dish", "?") for r in RECORDS])
def test_golden_recipe_is_valid(record):
    Recipe.model_validate(record)


@pytest.mark.parametrize("split", SPLITS)
def test_golden_dishes_are_unique(split):
    dishes = Counter(r["dish"].lower() for r in SPLITS[split])
    assert [d for d, n in dishes.items() if n > 1] == []


def test_dev_and_test_splits_do_not_overlap():
    dev = {r["dish"].lower() for r in SPLITS["dev"]}
    assert dev.isdisjoint(r["dish"].lower() for r in SPLITS["test"])

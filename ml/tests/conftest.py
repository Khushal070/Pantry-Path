"""Shared test fixtures. Tests never call a model server, an MLflow server or GCP.

Every test runs against a temporary copy of ml/configs and ml/prompts (so reports, caches
and evaluation sets land in a temp folder) and a temporary data-pipeline home with a small
fake catalog and fake golden recipes: no test reads the real held-out set.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from pantrypath_core.catalog import Catalog
from pantrypath_ml import config, registry
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.llm import LLMReply, LLMReplyError, Usage
from pantrypath_pipeline.store import build_sqlite

ML_HOME = Path(__file__).resolve().parents[1]
PIPELINE_HOME = ML_HOME.parent / "data-pipeline"


def ingredients(*names: str) -> list[dict]:
    """First name core, the rest common."""
    return [{"name": n, "role": "core" if i == 0 else "common", "category": "produce",
             "share": 0.5} for i, n in enumerate(names)]


CATALOG = [
    {"dish_id": "lasagna", "name": "lasagna", "aliases": ["beef lasagna", "vegan lasagna"],
     "cuisine": "european", "review_count": 900, "recipe_count": 40,
     "ingredients": ingredients("lasagna noodles", "ground beef", "ricotta cheese",
                                "tomato sauce")},
    {"dish_id": "pad-thai", "name": "pad thai", "aliases": [], "cuisine": "east_se_asian",
     "review_count": 700, "recipe_count": 30,
     "ingredients": ingredients("rice noodles", "peanuts", "fish sauce", "egg")},
    {"dish_id": "chicken-tikka-masala", "name": "chicken tikka masala", "aliases": [],
     "cuisine": "south_asian", "review_count": 500, "recipe_count": 20,
     "ingredients": ingredients("chicken", "yogurt", "garam masala")},
    {"dish_id": "hummus", "name": "hummus", "aliases": [], "cuisine": "middle_eastern_african",
     "review_count": 300, "recipe_count": 10,
     "ingredients": ingredients("chickpeas", "tahini", "lemon juice")},
    {"dish_id": "tacos", "name": "tacos", "aliases": ["beef tacos"], "cuisine": "latin_american",
     "review_count": 200, "recipe_count": 15,
     "ingredients": ingredients("tortillas", "ground beef", "salsa")},
    {"dish_id": "pancakes", "name": "pancakes", "aliases": ["flapjacks"], "cuisine": "american",
     "review_count": 100, "recipe_count": 50,
     "ingredients": ingredients("flour", "milk", "egg", "butter")},
]
GOLDEN_DEV = [
    {"dish": "Beef Lasagna", "ingredients": [
        {"name": "lasagna noodles"}, {"name": "ground beef"}, {"name": "ricotta cheese"},
        {"name": "salt"}, {"name": "basil", "optional": True}]},
    {"dish": "Pad Thai", "ingredients": [
        {"name": "rice noodles"}, {"name": "peanuts"}, {"name": "fish sauce"},
        {"name": "tofu"}]},
]
GOLDEN_TEST = [{"dish": "Hummus", "ingredients": [{"name": "chickpeas"}, {"name": "tahini"}]}]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No .env, no overrides, and every ml/ path inside a temp folder."""
    for name in ("MODEL_SERVER_URL", "MODEL_NAME", "MODEL_SERVER_TOKEN", "MODEL_SERVER_AUTH",
                 "MLFLOW_TRACKING_URI",
                 "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "ALERT_EMAIL_TO", "ALERT_SMTP_USER",
                 "ALERT_SMTP_PASSWORD", "SLACK_WEBHOOK_URL", "GCP_PROJECT_ID", "GCP_REGION",
                 "AR_DOCKER_REPO", "AR_GENERIC_REPO", "PANTRYPATH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANTRYPATH_ENV_FILE", "/nonexistent/.env")
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    home = tmp_path / "ml"
    shutil.copytree(ML_HOME / "configs", home / "configs")
    shutil.copytree(ML_HOME / "prompts", home / "prompts")
    data = yaml.safe_load((home / "configs" / "candidates.yaml").read_text())
    data["client"]["retry_wait_max_s"] = 0
    (home / "configs" / "candidates.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(config, "ML_HOME", home)
    monkeypatch.setattr(config, "REPORTS", home / "reports")
    monkeypatch.setattr(registry, "LOCAL_STORE", home / "mlruns")
    monkeypatch.setattr(registry, "git_commit", lambda: "abc123")


@pytest.fixture
def cfg() -> Config:
    """configs/candidates.yaml (temp copy, no retry waits)."""
    return config.load("candidates")


@pytest.fixture
def pcfg(tmp_path, monkeypatch) -> Config:
    """The real pipeline.yaml with a fake catalog (JSON + SQLite) and fake golden recipes."""
    home = tmp_path / "pipeline"
    data = yaml.safe_load((PIPELINE_HOME / "config" / "pipeline.yaml").read_text())
    shutil.copytree(PIPELINE_HOME / "prompts", home / "prompts")
    for name, rows in (("golden_dev.json", GOLDEN_DEV), ("golden_test.json", GOLDEN_TEST)):
        (home / name).write_text(json.dumps(rows))
    data["catalog_eval"]["splits"] = {"dev": "golden_dev.json", "test": "golden_test.json"}
    data["catalog"]["profiles_output"] = "dish_profiles.json"
    data["catalog"]["sqlite_output"] = "pantrypath.sqlite"
    dishes = [{**d, "source": "foodcom", "catalog_version": "test"} for d in CATALOG]
    catalog = Catalog(catalog_version="test", generated_at=datetime.now(UTC), dishes=dishes)
    (home / "dish_profiles.json").write_text(catalog.model_dump_json())
    build_sqlite(catalog, home / "pantrypath.sqlite")
    pipeline_cfg = Config(data, home)
    monkeypatch.setattr(config, "pipeline_config", lambda: pipeline_cfg)
    return pipeline_cfg


def reply(data: dict | None, *, text: str | None = None, cached: bool = False) -> LLMReply:
    return LLMReply(data=data or {}, usage=Usage(100, 50), model="fake-model", provider="fake",
                    cached=cached, raw_text=text if text is not None else json.dumps(data))


class ScriptedClient:
    """A fake LLMClient: `answer(prompt)` returns data, or raises with the given message."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, answer):
        self.answer = answer
        self.prompts: list[str] = []

    def complete_json(self, prompt, schema, *, purpose):
        self.prompts.append(prompt)
        data = self.answer(prompt)
        if isinstance(data, str):
            raise LLMReplyError(data, reply(None, text="oops"))
        return reply(data)


@pytest.fixture
def fake_server(monkeypatch):
    """Route cached_client's inner LocalLLMClient to a ScriptedClient."""
    from pantrypath_ml import local_llm

    def install(answer):
        scripted = ScriptedClient(answer)
        monkeypatch.setattr(local_llm, "make_client", lambda *a, **k: scripted)
        return scripted
    return install


def refine_reply(name: str, ingredient_names: list[str], *, keep: bool = True) -> dict:
    """A REFINE_SCHEMA reply listing ingredients as LLM additions (first one core)."""
    return {"keep": keep, "merge_into": None, "name": name, "aliases": [], "cuisine": "american",
            "ingredients": [],
            "added": [{"name": n, "role": "core" if i == 0 else "common", "category": "produce",
                       "why": "needed"} for i, n in enumerate(ingredient_names)]}


def dish_name(prompt: str) -> str:
    """The dish a refine prompt asks about (describe_dish's 'name: ...' line)."""
    lines = prompt.splitlines()
    return next(line[6:] for line, after in zip(lines, lines[1:], strict=False)
                if line.startswith("name: ") and after.startswith("cuisine hint"))

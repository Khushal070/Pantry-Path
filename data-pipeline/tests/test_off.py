"""Open Food Facts snapshot: download checks, filtering, barcode lookup.

The fixture dump mimics the real Hugging Face export: product names and ingredient
statements are lists of {lang, text}, tags are lists of strings.
"""

import subprocess

import duckdb
import httpx2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from huggingface_hub.errors import RepositoryNotFoundError

from pantrypath_core.barcodes import gtin_check_digit, kroger_upc_to_gtin13
from pantrypath_pipeline import off_lookup
from pantrypath_pipeline.acquire import off as acquire
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError
from pantrypath_pipeline.preprocess import off_filter

LANG_TEXT = pa.list_(pa.struct([("lang", pa.string()), ("text", pa.string())]))
TAGS = pa.list_(pa.string())
DUMP_SCHEMA = pa.schema([
    ("code", pa.string()), ("lang", pa.string()), ("product_name", LANG_TEXT),
    ("brands", pa.string()), ("categories_tags", TAGS), ("countries_tags", TAGS),
    ("allergens_tags", TAGS), ("traces_tags", TAGS), ("ingredients_text", LANG_TEXT),
    ("labels_tags", TAGS), ("ingredients_analysis_tags", TAGS), ("unique_scans_n", pa.int32()),
    ("completeness", pa.float32()), ("last_modified_t", pa.int64()),
])
US = ["en:united-states"]


def ean13(body12: str) -> str:
    return body12 + gtin_check_digit(body12)


EGGS = "0011110609038"          # Kroger Large Eggs
MILK_ONLY = ean13("400638133393")
FRENCH = ean13("761234567890")
SPANISH = ean13("750123456789")
ENGLISH = ean13("001234567890")
NO_SCANS = ean13("002345678901")
MARKED_UP = ean13("004567890123")


def product(code, *, ing=None, allergens=(), countries=US, lang="en", scans=1,
            modified=1_700_000_000, name="Product", categories=("en:snacks",), **extra):
    """One dump row; `ing` is a plain English string or a list of {lang, text}."""
    if isinstance(ing, str):
        ing = [{"lang": "main", "text": ing}, {"lang": "en", "text": ing}]
    row = {
        "code": code, "lang": lang,
        "product_name": None if name is None else [{"lang": "main", "text": name}],
        "brands": "Brand", "categories_tags": None if categories is None else list(categories),
        "countries_tags": list(countries),
        "allergens_tags": list(allergens), "traces_tags": [], "ingredients_text": ing or [],
        "labels_tags": [], "ingredients_analysis_tags": ["en:non-vegan"],
        "unique_scans_n": scans, "completeness": 0.5, "last_modified_t": modified,
    }
    row.update(extra)
    return row


DUMP_ROWS = [
    product(EGGS, ing="Eggs.", allergens=["en:eggs"], scans=10, name="Large Eggs",
            brands="Kroger", categories=("en:eggs",)),
    product(MILK_ONLY, allergens=["en:milk"], name=None, categories=None),
    product(ean13("963850700000"), name="No evidence"),
    product(ean13("500011263792"), ing="Water.", countries=["en:france"]),
    product("0036000291453", ing="Sugar."),                       # wrong check digit
    product("036000291452", ing="Corn.", scans=5),                 # three forms of one barcode
    product("0036000291452", ing="Corn.", scans=50, modified=1_600_000_000),
    product("00036000291452", ing="Corn, salt.", scans=50, modified=1_700_000_000),
    product(FRENCH, lang="fr", ing=[{"lang": "main", "text": "Lait, sucre"},
                                    {"lang": "fr", "text": "Lait, sucre"}]),
    product(SPANISH, lang="es", ing=[{"lang": "de", "text": "Zucker"},
                                     {"lang": "main", "text": "Azúcar"}]),
    product(ENGLISH, ing=[{"lang": "main", "text": "Main text"},
                          {"lang": "en", "text": "  English text  "}]),
    product(ean13("003456789012"), ing=[{"lang": "en", "text": "   "}]),  # blank = no evidence
    product(NO_SCANS, ing="Rice.", scans=None),
    product("128501447328300349", ing="Flour."),                   # store-internal code
    product(MARKED_UP, name="Mac &amp; Cheese",
            ing='<span class="allergen">Milk</span>, _wheat_ flour, &quot;natural&quot; flavor'),
    product(ean13("005678901234"), ing="<br/>"),                    # nothing left after cleaning
]


def write_dump(path, rows=DUMP_ROWS, schema=DUMP_SCHEMA):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path)
    return path


@pytest.fixture
def off_cfg(cfg):
    cfg["off"].update(min_raw_bytes=10, keep_raw=True, min_output_rows=1, retry_wait_s=0)
    return cfg


@pytest.fixture
def dump(off_cfg):
    return write_dump(acquire.raw_file(off_cfg))


def snapshot_rows(cfg):
    path = cfg.path(cfg["off"]["output"])
    with duckdb.connect() as con:
        cursor = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [d[0] for d in cursor.description]
        return {r[0]: dict(zip(names, r, strict=True)) for r in cursor.fetchall()}


# ---------------------------------------------------------------- filter

def test_filter_keeps_us_products_with_evidence(off_cfg, dump):
    summary = off_filter.run(off_cfg)
    rows = snapshot_rows(off_cfg)
    # French and Spanish ingredient text: dropped (the diet rules read English only)
    assert set(rows) == {EGGS, MILK_ONLY, "0036000291452", ENGLISH, NO_SCANS, MARKED_UP}
    assert summary["rows"] == 6
    assert summary["evidence_rows"] == 12
    assert summary["invalid_barcodes"] == 2
    assert summary["other_language_removed"] == 2
    assert summary["duplicates_removed"] == 2


def test_filter_keeps_other_languages_when_configured(off_cfg, dump):
    off_cfg.data["off"]["ingredient_languages"] = ["en", "fr", "es"]
    summary = off_filter.run(off_cfg)
    assert FRENCH in snapshot_rows(off_cfg) and SPANISH in snapshot_rows(off_cfg)
    assert (summary["rows"], summary["other_language_removed"]) == (8, 0)


def test_filter_cleans_markup_for_keyword_matching(off_cfg, dump):
    off_filter.run(off_cfg)
    row = snapshot_rows(off_cfg)[MARKED_UP]
    assert row["ingredients_text"] == 'Milk, wheat flour, "natural" flavor'
    assert row["product_name"] == "Mac & Cheese"


@pytest.mark.parametrize("raw, clean", [
    (None, None),
    ("<br/>", None),
    ("Sugar, <span class='allergen'>Eggs</span>", "Sugar, Eggs"),
    ("Contains <2% of salt", "Contains <2% of salt"),
    ("Flour (<b>wheat</b> ) , salt .", "Flour (wheat), salt."),
    ("_Wheat_  Flour,\r\n\r\nWater", "Wheat Flour, Water"),
    ("Wheatflour_contains Gluten", "Wheatflour contains Gluten"),
    ("&lt;2% &amp; more", "<2% & more"),
])
def test_clean_text(raw, clean):
    assert off_filter.clean_text(raw) == clean


def test_filter_output_columns_and_types(off_cfg, dump):
    off_filter.run(off_cfg)
    table = pq.read_table(off_cfg.path(off_cfg["off"]["output"]))
    assert tuple(table.column_names) == off_filter.OUTPUT_COLUMNS
    assert table.schema.field("unique_scans_n").type == pa.int32()
    tags = table.schema.field("allergens_tags").type
    assert pa.types.is_list(tags) and tags.value_type == pa.string()
    assert table.column("barcode").to_pylist() == sorted(table.column("barcode").to_pylist())


def test_filter_dedup_keeps_most_scanned_then_newest(off_cfg, dump):
    off_filter.run(off_cfg)
    row = snapshot_rows(off_cfg)["0036000291452"]
    assert row["code"] == "00036000291452"
    assert row["ingredients_text"] == "Corn, salt."


def test_filter_picks_english_then_main_language(off_cfg, dump):
    off_cfg.data["off"]["ingredient_languages"] = ["en", "fr", "es"]  # keep them to inspect
    off_filter.run(off_cfg)
    rows = snapshot_rows(off_cfg)
    assert (rows[ENGLISH]["ingredients_text"], rows[ENGLISH]["ingredients_lang"]) == (
        "English text", "en")
    assert (rows[SPANISH]["ingredients_text"], rows[SPANISH]["ingredients_lang"]) == (
        "Azúcar", "es")
    assert rows[FRENCH]["ingredients_lang"] == "fr"


def test_filter_handles_missing_values(off_cfg, dump):
    off_filter.run(off_cfg)
    rows = snapshot_rows(off_cfg)
    assert rows[MILK_ONLY]["ingredients_text"] is None
    assert rows[MILK_ONLY]["product_name"] is None
    assert rows[MILK_ONLY]["categories_tags"] == []
    assert rows[NO_SCANS]["unique_scans_n"] == 0


def test_filter_writes_atomically(off_cfg, dump):
    off_filter.run(off_cfg)
    output = off_cfg.path(off_cfg["off"]["output"])
    assert output.exists()
    assert not output.with_name(output.name + ".tmp").exists()


def test_filter_without_analysis_tags_column(off_cfg):
    schema = DUMP_SCHEMA.remove(DUMP_SCHEMA.get_field_index("ingredients_analysis_tags"))
    rows = [{k: v for k, v in r.items() if k != "ingredients_analysis_tags"} for r in DUMP_ROWS]
    write_dump(acquire.raw_file(off_cfg), rows, schema)
    off_filter.run(off_cfg)
    assert snapshot_rows(off_cfg)[EGGS]["ingredients_analysis_tags"] == []


@pytest.mark.parametrize("keep_raw, min_rows, deleted", [
    (False, 1, True), (False, 100, False), (True, 1, False),
])
def test_raw_dump_deleted_only_when_safe(off_cfg, dump, keep_raw, min_rows, deleted):
    off_cfg["off"].update(keep_raw=keep_raw, min_output_rows=min_rows)
    assert off_filter.run(off_cfg)["raw_deleted"] is deleted
    assert dump.exists() is not deleted


def test_filter_without_dump_keeps_previous_snapshot(off_cfg, dump):
    off_filter.run(off_cfg)
    dump.unlink()
    assert "skipped" in off_filter.run(off_cfg)


def test_filter_without_dump_or_snapshot_fails(off_cfg):
    with pytest.raises(DataUnavailableError, match="download-off"):
        off_filter.run(off_cfg)


# ---------------------------------------------------------------- download

class FakeHub:
    """Stands in for hf_hub_download: plays back one action per call."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = []

    def __call__(self, *, repo_id, filename, repo_type, local_dir, force_download):
        self.calls.append(force_download)
        action = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        if isinstance(action, BaseException):
            raise action
        path = local_dir / filename
        action(path)
        return str(path)


def good(path):
    write_dump(path)


def corrupt(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PAR1" + b"\0" * 200)  # right magic bytes, no readable footer


@pytest.fixture
def hub(monkeypatch):
    def install(*actions):
        fake = FakeHub(*actions)
        monkeypatch.setattr(acquire, "hf_hub_download", fake)
        return fake
    return install


def test_download_returns_row_count(off_cfg, hub):
    fake = hub(good)
    summary = acquire.run(off_cfg)
    assert summary["rows"] == len(DUMP_ROWS)
    assert fake.calls == [False]


def test_corrupt_download_is_replaced_once(off_cfg, hub):
    fake = hub(corrupt, good)
    assert acquire.run(off_cfg)["rows"] == len(DUMP_ROWS)
    assert fake.calls == [False, True]  # the second download is forced


def test_corrupt_twice_fails_and_removes_the_file(off_cfg, hub):
    hub(corrupt)
    with pytest.raises(DataCorruptError, match="not a readable Parquet"):
        acquire.run(off_cfg)
    assert not acquire.raw_file(off_cfg).exists()


def test_too_small_download_is_corrupt(off_cfg, hub):
    off_cfg["off"]["min_raw_bytes"] = 10**9
    hub(good)
    with pytest.raises(DataCorruptError, match="min_raw_bytes"):
        acquire.run(off_cfg)


def test_dump_missing_columns_is_corrupt(tmp_path):
    path = tmp_path / "food.parquet"
    pq.write_table(pa.table({"code": ["1"]}), path)
    with pytest.raises(DataCorruptError, match="lacks columns"):
        acquire.check_raw_file(path, min_bytes=1)


def test_unreachable_source_is_retried_then_fails(off_cfg, hub):
    fake = hub(OSError("connection reset"))
    with pytest.raises(DataUnavailableError, match="after 3 attempts"):
        acquire.run(off_cfg)
    assert len(fake.calls) == 3


def test_unreachable_source_falls_back_to_previous_snapshot(off_cfg, hub, monkeypatch):
    alerts = []
    monkeypatch.setattr(acquire, "send_alert", lambda *a, **kw: alerts.append(a))
    snapshot = off_cfg.path(off_cfg["off"]["output"])
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"previous")
    hub(OSError("connection reset"))
    assert acquire.run(off_cfg)["fallback"] == "previous snapshot"
    assert alerts and "previous snapshot" in alerts[0][0]


def test_wrong_repo_is_not_retried(off_cfg, hub):
    response = httpx2.Response(404, request=httpx2.Request("GET", "https://huggingface.co/x"))
    fake = hub(RepositoryNotFoundError("Repository not found", response=response))
    with pytest.raises(DataUnavailableError, match="off.hf_repo"):
        acquire.run(off_cfg)
    assert len(fake.calls) == 1


def test_dvc_source_pulls_the_snapshot(off_cfg, monkeypatch):
    off_cfg["off"]["source"] = "dvc"
    target = off_cfg.path(off_cfg["off"]["output"])

    def fake_run(cmd, **kwargs):
        assert cmd[1:4] == ["-m", "dvc", "pull"] and cmd[-1] == str(target) + ".dvc"
        target.write_bytes(b"snapshot")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(acquire.subprocess, "run", fake_run)
    assert acquire.run(off_cfg)["source"] == "dvc"


def test_dvc_pull_failure_is_reported(off_cfg, monkeypatch):
    off_cfg["off"]["source"] = "dvc"
    monkeypatch.setattr(acquire.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "ERROR: failed to pull data from the cloud"))
    with pytest.raises(DataUnavailableError, match="failed to pull"):
        acquire.run(off_cfg)


# ---------------------------------------------------------------- lookup

def test_lookup_finds_any_barcode_form(off_cfg, dump):
    off_filter.run(off_cfg)
    found = off_lookup.lookup(off_cfg, [
        "036000291452",                          # UPC-A as printed on the pack
        MILK_ONLY,
        kroger_upc_to_gtin13("0001111060903"),   # Kroger's code, converted first
        "not-a-barcode",
        ean13("999999999999"),                   # valid but not in the snapshot
    ])
    assert set(found) == {"0036000291452", MILK_ONLY, EGGS}
    eggs = found[EGGS]
    assert (eggs.source, eggs.name, eggs.brand) == ("off", "Large Eggs", "Kroger")
    assert eggs.allergens == ["en:eggs"] and eggs.categories == ["en:eggs"]
    assert eggs.ingredients_text == "Eggs."
    assert found[MILK_ONLY].ingredients_text == ""


def test_lookup_drops_non_english_ingredient_text(off_cfg, dump):
    off_cfg.data["off"]["ingredient_languages"] = ["en", "fr"]  # a snapshot that kept French
    off_filter.run(off_cfg)
    assert off_lookup.lookup(off_cfg, [FRENCH])[FRENCH].ingredients_text == ""


def test_lookup_without_valid_barcodes_skips_the_file(off_cfg):
    assert off_lookup.lookup(off_cfg, ["abc", ""]) == {}


def test_lookup_without_snapshot_fails(off_cfg):
    with pytest.raises(DataUnavailableError, match="dvc pull"):
        off_lookup.lookup(off_cfg, [EGGS])


def test_evidence_from_pandas_style_row():
    row = {"product_name": float("nan"), "brands": None, "ingredients_text": "Milk, sugar.",
           "categories_tags": np.array(["en:desserts"]), "allergens_tags": float("nan"),
           "traces_tags": np.array([], dtype=object)}
    evidence = off_lookup.evidence_from_row(row)
    assert evidence.name == "" and evidence.brand == ""
    assert evidence.categories == ["en:desserts"] and evidence.allergens == []
    assert evidence.ingredients_text == "Milk, sugar."   # no language column: text is kept


def test_evidence_with_unknown_language_has_no_text():
    row = {"ingredients_text": "Lait", "ingredients_lang": None}
    assert off_lookup.evidence_from_row(row).ingredients_text == ""

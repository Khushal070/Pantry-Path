"""Filter the Open Food Facts dump down to the US allergen snapshot.

From ~4.8M products worldwide we keep US products that carry evidence (an ingredient
statement or declared allergens): ~460k rows, ~90 MB instead of 8 GB. Along the way:

- barcodes become GTIN-13 (pantrypath_core.barcodes.to_gtin13, the same rule the
  request flow uses), and rows whose code isn't a valid retail barcode are dropped;
- product names and ingredient statements are lists of {lang, text}: we keep the
  English text, else the product's main-language text, else the first non-empty one,
  and record that language in ingredients_lang. The diet rules read English only, so
  products whose ingredient text is in another language are dropped (off.ingredient_languages;
  ~0.5% of US products, but a batch of Norwegian ones arrived in late October 2026);
- text is cleaned for keyword matching: Open Food Facts marks allergens with HTML
  (<span class="allergen">Milk</span>, ~23% of statements) or underscores (_milk_,
  which a \\bmilk\\b search would miss), and some texts carry HTML entities (&quot;);
- one row per barcode: the most scanned, then the most recently edited.

The output is written to a temp file and renamed, so a crash never leaves half a file.
"""

import html
import logging
import os
import re
import time
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from pantrypath_core.barcodes import to_gtin13
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError

log = logging.getLogger(__name__)

# Output columns, in order. Other steps (validation, bias, request lookup) rely on these names.
OUTPUT_COLUMNS = (
    "barcode", "code", "product_name", "brands", "categories_tags", "allergens_tags",
    "traces_tags", "ingredients_text", "ingredients_lang", "labels_tags",
    "ingredients_analysis_tags", "unique_scans_n", "completeness", "last_modified_t",
)


_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")  # tags only: "<2% of salt" is left alone
_SPACE_BEFORE_PUNCT = re.compile(r" ([,.;:)\]])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[]) ")


def clean_text(text: str | None) -> str | None:
    """Strip HTML tags, decode entities, drop _allergen_ underscores, collapse whitespace."""
    if text is None:
        return None
    text = html.unescape(_HTML_TAG.sub(" ", text)).replace("_", " ")
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(text.split()))
    return _SPACE_AFTER_OPEN.sub(r"\1", text) or None


def _pick_text(column: str) -> str:
    """SQL for the best text in a list of {lang, text}: English, then main language, then any."""
    return (
        f"clean_text(coalesce(list_filter({column}, lambda x: x.lang = 'en')[1].text, "
        f"list_filter({column}, lambda x: x.lang = 'main')[1].text, {column}[1].text))"
    )


def _select_sql(has_analysis_tags: bool) -> str:
    # Older dumps may lack ingredients_analysis_tags; the column is then empty lists
    source_col = "ingredients_analysis_tags," if has_analysis_tags else ""
    analysis = (
        "coalesce(ingredients_analysis_tags, []::VARCHAR[])"
        if has_analysis_tags else "[]::VARCHAR[]"
    )
    non_empty = "lambda x: nullif(trim(x.text), '') IS NOT NULL"
    return f"""
    WITH us AS (
        SELECT
            code, lang, brands, categories_tags, allergens_tags, traces_tags, labels_tags,
            {source_col}
            unique_scans_n, completeness, last_modified_t,
            list_filter(product_name, {non_empty}) AS names,
            list_filter(ingredients_text, {non_empty}) AS ing
        FROM read_parquet($raw)
        WHERE list_contains(countries_tags, $country)
    ), cleaned AS (
        SELECT
            to_gtin13(code) AS barcode,
            code,
            {_pick_text("names")} AS product_name,
            nullif(trim(brands), '') AS brands,
            coalesce(categories_tags, []::VARCHAR[]) AS categories_tags,
            coalesce(allergens_tags, []::VARCHAR[]) AS allergens_tags,
            coalesce(traces_tags, []::VARCHAR[]) AS traces_tags,
            {_pick_text("ing")} AS ingredients_text,
            CASE
                WHEN len(list_filter(ing, lambda x: x.lang = 'en')) > 0 THEN 'en'
                WHEN len(list_filter(ing, lambda x: x.lang = 'main')) > 0 THEN lang
                ELSE ing[1].lang
            END AS ingredients_lang,
            coalesce(labels_tags, []::VARCHAR[]) AS labels_tags,
            {analysis} AS ingredients_analysis_tags,
            coalesce(unique_scans_n, 0)::INTEGER AS unique_scans_n,
            completeness,
            last_modified_t
        FROM us
        WHERE len(ing) > 0 OR len(allergens_tags) > 0
    )
    -- Evidence = an ingredient statement that survives cleaning, or declared allergens
    SELECT * FROM cleaned WHERE ingredients_text IS NOT NULL OR len(allergens_tags) > 0
    """


# Keep a product when its ingredient text is in a language the diet rules read. Products
# without text (allergen tags only) are kept: the tags don't depend on the language.
def language_ok(languages: list[str]) -> str:
    """SQL condition: no ingredient text, or text in one of these languages."""
    quoted = ", ".join("'" + lang.replace("'", "''") + "'" for lang in languages)
    return f"(ingredients_text IS NULL OR list_contains([{quoted}], ingredients_lang))"


def _sql_path(path: Path) -> str:
    """A path quoted for a SQL string literal (COPY and SET don't take parameters)."""
    return str(path).replace("'", "''")


def connect(cfg: Config) -> duckdb.DuckDBPyConnection:
    """DuckDB with a memory cap (Docker) and spill space under data/interim."""
    con = duckdb.connect()
    tmp = cfg.path(cfg["paths"]["interim"]) / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit = '{cfg['off'].get('duckdb_memory_limit', '4GB')}'")
    con.execute(f"SET temp_directory = '{_sql_path(tmp)}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET enable_progress_bar = false")  # keeps task logs readable
    return con


def run(cfg: Config, **_: object) -> dict:
    off = cfg["off"]
    raw = cfg.path(off["raw_dir"]) / off["hf_file"]
    output = cfg.path(off["output"])
    if not raw.exists():
        if output.exists():
            log.info("No new dump at %s; keeping the previous snapshot %s", raw, output)
            return {"skipped": "no new dump; previous snapshot kept", "snapshot": str(output)}
        raise DataUnavailableError(
            f"Neither the dump {raw} nor a snapshot {output} exists: run download-off first"
        )

    started = time.monotonic()
    con = connect(cfg)
    # "special" null handling: both functions may return NULL
    for name, func in (("to_gtin13", to_gtin13), ("clean_text", clean_text)):
        con.create_function(
            name, func, ["VARCHAR"], "VARCHAR", null_handling="special", side_effects=False
        )
    has_analysis = "ingredients_analysis_tags" in pq.ParquetFile(raw).schema_arrow.names
    con.execute(
        f"CREATE TEMP TABLE evidence AS {_select_sql(has_analysis)}",
        {"raw": str(raw), "country": off["country_tag"]},
    )
    keep = language_ok(list(off.get("ingredient_languages") or ["en"]))
    evidence_rows, invalid, other_language = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE barcode IS NULL), "
        f"count(*) FILTER (WHERE barcode IS NOT NULL AND NOT {keep}) FROM evidence"
    ).fetchone()

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output.with_name(output.name + ".tmp")
    columns = ", ".join(OUTPUT_COLUMNS)
    # Sorted by barcode so lookups can skip row groups using Parquet min/max statistics
    con.execute(f"""
        COPY (
            SELECT {columns} FROM evidence
            WHERE barcode IS NOT NULL AND {keep}
            QUALIFY row_number() OVER (
                PARTITION BY barcode
                ORDER BY unique_scans_n DESC, last_modified_t DESC NULLS LAST, code
            ) = 1
            ORDER BY barcode
        ) TO '{_sql_path(tmp_out)}' (FORMAT parquet, COMPRESSION zstd)
    """)
    os.replace(tmp_out, output)
    con.close()

    rows = pq.ParquetFile(output).metadata.num_rows
    summary = {
        "rows": rows,
        "evidence_rows": evidence_rows,
        "invalid_barcodes": invalid,
        "other_language_removed": other_language,
        "duplicates_removed": evidence_rows - invalid - other_language - rows,
        "bytes": output.stat().st_size,
        "seconds": round(time.monotonic() - started, 1),
        "raw_deleted": _maybe_delete_raw(raw, rows, off),
    }
    log.info("Open Food Facts snapshot written to %s: %s", output, summary)
    return summary


def _maybe_delete_raw(raw: Path, rows: int, off: dict) -> bool:
    """Delete the 8 GB dump only if asked to and the snapshot looks complete."""
    if off.get("keep_raw", False):
        return False
    minimum = int(off.get("min_output_rows", 0))
    if rows < minimum:
        log.warning(
            "Snapshot has %d rows, below off.min_output_rows (%d): keeping %s for inspection",
            rows, minimum, raw,
        )
        return False
    raw.unlink()
    log.info("Deleted the raw dump %s", raw)
    return True

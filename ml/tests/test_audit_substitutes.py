"""audit_substitutes on a tiny fake Open Food Facts snapshot (no network, no real data)."""

import duckdb

from pantrypath_ml import audit_substitutes as audit


def snapshot(tmp_path, rows):
    path = tmp_path / "off.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (product_name VARCHAR, allergens_tags VARCHAR[], "
                "traces_tags VARCHAR[], ingredients_text VARCHAR, "
                "ingredients_analysis_tags VARCHAR[])")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    return path


def test_flags_an_allergen_missing_from_contains(tmp_path):
    rows = [("Seitan", ["en:gluten", "en:soybeans"], [], "wheat gluten, soy sauce", [])] * 3
    rows += [("Traditional Seitan 8 oz", ["en:gluten"], [], "wheat gluten, water", [])] * 3
    rows += [("Seitan Chocolate Bar", ["en:milk"], [], "cocoa, milk", [])] * 10  # not seitan
    table = tmp_path / "substitutes.yaml"
    table.write_text("substitutes:\n  chicken:\n    - {name: seitan, contains: [wheat, gluten]}\n"
                     "  egg:\n    - {name: flax egg, contains: []}\n")
    result = {r["name"]: r for r in audit.run(snapshot(tmp_path, rows), table)["results"]}
    seitan = result["seitan"]
    assert seitan["matched"] == 6                      # the chocolate bar is not seitan
    assert [(f["allergen"], f["share"]) for f in seitan["flags"]] == [("soy", 0.5)]
    assert not result["flax egg"]["auditable"]          # a home preparation: no products


def test_matching_keeps_plain_products_and_drops_dishes_made_with_them():
    import re

    pattern = re.compile(audit.phrase_regex("coconut milk"))
    for name, hit in (("organic coconut milk unsweetened", True), ("coconut milks", True),
                      ("coconut milk chocolate", False), ("coconut-milk", True)):
        assert bool(pattern.search(audit.clean_name(name))) is hit, name
    assert audit.clean_name("Rice Noodles 16 oz (2 pack)") == "rice noodles"
    assert audit.base_name("soy milk with lemon juice") == "soy milk"


def test_traces_and_diet_tags():
    rows = [("x", [], ["en:peanuts"], "sunflower seeds", ["en:non-vegan"])] * 5
    out = audit.score(rows, set(), declared_min=0.1, trace_min=0.2, min_products=5)
    assert {(f["allergen"], f["kind"]) for f in out["flags"]} == {("peanuts", "trace"),
                                                                  ("animal", "diet")}
    assert not audit.score(rows, {"peanuts", "animal"}, declared_min=0.1, trace_min=0.2,
                           min_products=5)["flags"]

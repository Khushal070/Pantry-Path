"""A1 generator (determinism, split by dish, consistent labels) and A1 metrics."""

import csv
import json

import pytest
from conftest import CATALOG, ML_HOME

from pantrypath_ml import config, evaluate_a1
from pantrypath_ml.evalsets import (
    HeldOutError,
    generate_a1,
    load_a1,
    typo,
    write_a1,
)
from pantrypath_ml.parse_request import CrossCheck


@pytest.fixture
def gcfg():
    return config.load("evalsets")["a1"]


def test_generator_is_deterministic(gcfg):
    assert generate_a1(CATALOG, gcfg) == generate_a1(CATALOG, gcfg)
    other = generate_a1(CATALOG, {**gcfg, "seed": gcfg["seed"] + 1})
    assert other != generate_a1(CATALOG, gcfg)


def test_split_has_no_dish_overlap(gcfg):
    splits = generate_a1(CATALOG, gcfg)
    dev = {c["dish_id"] for c in splits["dev"]}
    test = {c["dish_id"] for c in splits["test"]}
    assert dev and test and not dev & test
    assert len(test) == round(len(CATALOG) * gcfg["test_fraction"])
    assert all(len(c["messages"]) in (1, 3) for s in splits.values() for c in s)


def test_labels_are_consistent(gcfg):
    cases = [c for s in generate_a1(CATALOG, gcfg).values() for c in s]
    assert {c["slices"]["style"] for c in cases} == set(gcfg["styles"])
    for c in cases:
        e = c["expected"]
        assert len(e["zip"]) == 5 and e["zip"].isdigit()
        assert e["zip"] in " ".join(m["content"] for m in c["messages"])
        assert e["diet"] in ("none", "vegetarian", "vegan")
        if "vegan" in c["dish_text"].split():
            assert e["diet"] == "vegan"  # "vegan lasagna" asks for vegan


def test_files_and_review_sample(pcfg, gcfg):
    summary = write_a1(pcfg, config.load("evalsets"))
    assert summary["dev"] + summary["test"] == len(CATALOG) * gcfg["cases_per_dish"]
    with open(summary["review_sample"]) as f:
        rows = list(csv.DictReader(f))
    assert rows and {"chat", "expected_avoid", "reviewer_ok"} <= set(rows[0])
    with pytest.raises(HeldOutError):
        load_a1(config.load("evalsets"), "test")
    assert len(load_a1(config.load("evalsets"), "dev")) == summary["dev"]


def test_typo_changes_one_word():
    import random

    out = typo("chicken tikka masala", random.Random(1))
    assert out != "chicken tikka masala" and len(out.split()) == 3


def test_few_shot_dishes_are_not_catalog_dishes(pcfg):
    resolve = evaluate_a1.dish_resolver(pcfg)
    prcfg = config.load("parse_request")
    for example in [*prcfg["few_shot_examples"], *prcfg["rules_v2_examples"]]:
        assert resolve(example["reply"]["query"]) is None


# ---------------------------------------------------------------- metrics


def case(avoid, diet="none", unsupported=()):
    return {"case_id": "c", "dish_id": "pad-thai", "split": "dev", "messages": [],
            "expected": {"zip": "02115", "avoid": avoid, "diet": diet, "servings": None,
                         "unsupported": list(unsupported)},
            "slices": {"style": "casual", "allergens": avoid, "diet": diet,
                       "has_unsupported": bool(unsupported)}}


def result(avoid, diet="none", suggested=None, status="ok", unsupported=(), unmentioned=()):
    return {"status": status, "fields": {"query": "pad thai", "zip": "02115", "avoid": avoid,
                                         "diet": diet},
            "extras": {"servings": None, "unsupported": list(unsupported)},
            "model_unsupported": [], "suggested": suggested, "unmentioned": list(unmentioned)}


def test_cross_check_counts_as_captured_but_not_exact():
    score = evaluate_a1.score_case(
        case(["milk", "peanuts"]),
        result(["peanuts"], status="needs_confirmation",
               suggested={"avoid": ["milk", "peanuts"], "diet": "none"}), "pad-thai")
    assert score["allergens_captured"] and not score["allergen_exact"]
    assert score["model_missed"] and score["caught"] and not score["false_confirmation"]
    assert score["per_allergen"] == {"milk": {"model": False, "captured": True},
                                     "peanuts": {"model": True, "captured": True}}


def test_extra_allergens_precision_and_flags():
    records = []
    for expected, model, unmentioned in ((["milk"], ["milk", "soy", "egg"], ["soy", "egg"]),
                                         (["egg"], ["egg", "fish"], []),
                                         (["egg"], ["egg"], [])):
        c = case(expected)
        status = "needs_confirmation" if unmentioned else "ok"
        r = result(model, status=status, unmentioned=unmentioned)
        records.append({**c, "result": r, "score": evaluate_a1.score_case(c, r, "pad-thai"),
                        "calls": 1, "latency_s": 1.0, "cached_calls": 0, "input_tokens": 5,
                        "output_tokens": 5})
    summary = evaluate_a1.summarize(records, with_slices=False)
    assert summary["allergen_precision"] == round(3 / 6, 4)
    assert summary["extra_allergen_rate"] == round(2 / 3, 4)
    assert summary["extra_flag_rate"] == 0.5
    # asking about an extra allergen is not a false confirmation
    assert summary["false_confirmation_rate"] == 0.0
    exact = evaluate_a1.score_case(case(["egg"]), result(["egg"], status="needs_confirmation"),
                                   "pad-thai")
    assert exact["false_confirmation"]


def test_a_justified_zip_confirmation_is_not_false():
    wrong_zip = result(["milk"], status="needs_confirmation")
    wrong_zip["fields"]["zip"] = "99999"
    assert not evaluate_a1.score_case(case(["milk"]), wrong_zip, "pad-thai")["false_confirmation"]
    right = result(["milk"], status="needs_confirmation")
    assert evaluate_a1.score_case(case(["milk"]), right, "pad-thai")["false_confirmation"]


def test_a_nuts_question_is_reported_apart_from_false_confirmations():
    nuts = {**result(["tree_nuts"], status="needs_confirmation"), "clarify": ["peanuts"],
            "conflicts": ["the chat mentions nuts, which can mean tree nuts or peanuts: "
                          "the suggested fix avoids both"]}
    score = evaluate_a1.score_case(case(["tree_nuts"]), nuts, "pad-thai")
    assert score["ambiguous_confirmation"] and not score["false_confirmation"]
    # with another conflict as well, the confirmation is judged as usual
    both = {**nuts, "conflicts": [*nuts["conflicts"], "another conflict"]}
    score = evaluate_a1.score_case(case(["tree_nuts"]), both, "pad-thai")
    assert score["false_confirmation"] and not score["ambiguous_confirmation"]
    records = [{"result": nuts, "score": evaluate_a1.score_case(case(["tree_nuts"]), nuts,
                                                                "pad-thai"),
                "calls": 1, "latency_s": 1.0}]
    summary = evaluate_a1.summarize(records, with_slices=False)
    assert (summary["false_confirmation_rate"], summary["ambiguous_confirmation_rate"]) \
        == (0.0, 1.0)


def test_realistic_split_is_held_out_and_well_labelled():
    ecfg = config.load("evalsets")
    with pytest.raises(HeldOutError):
        load_a1(ecfg, "realistic")
    rows = [json.loads(line) for line in
            (ML_HOME / "evalsets" / "a1_realistic.jsonl").read_text().splitlines()]
    dev_dishes = {json.loads(line)["dish_id"] for line in
                  (ML_HOME / "evalsets" / "a1_dev.jsonl").read_text().splitlines()}
    allergens = set(CrossCheck(config.load("parse_request")["cross_check"]).category)
    labels = set(config.load("parse_request")["unsupported_labels"])
    assert len(rows) >= 40 and len({r["case_id"] for r in rows}) == len(rows)
    for r in rows:
        e, text = r["expected"], " ".join(m["content"] for m in r["messages"])
        assert r["split"] == "realistic" and r["dish_id"] in dev_dishes  # no test dishes
        assert e["zip"] is None or e["zip"] in text
        assert set(e["avoid"]) <= allergens and set(e["unsupported"]) <= labels
        assert e["diet"] in ("none", "vegetarian", "vegan")
    # the four paraphrases of the cross-check fixes are left out of the metrics and gate
    excluded = set(ecfg["a1"]["realistic_excluded"])
    assert len(excluded) == 4 and excluded <= {r["case_id"] for r in rows}
    target = config.ML_HOME / "evalsets" / "a1_realistic.jsonl"  # the test's temp home
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((ML_HOME / "evalsets" / "a1_realistic.jsonl").read_text())
    loaded = load_a1(ecfg, "realistic", allow_test=True)
    assert len(loaded) == len(rows) - 4 and not excluded & {c["case_id"] for c in loaded}


def test_limit_samples_across_styles():
    from pantrypath_ml.reporting import sample

    items = [{"id": i, "style": "abc"[i % 3] if i < 9 else "a"} for i in range(20)]
    picked = sample(items, 6, key=lambda c: c["style"])
    assert len(picked) == 6 and {c["style"] for c in picked} == {"a", "b", "c"}
    assert [c["id"] for c in picked] == sorted(c["id"] for c in picked)  # original order
    assert picked == sample(items, 6, key=lambda c: c["style"])  # seeded
    assert sample(items, None, key=lambda c: c["style"]) == items


def test_invalid_reply_misses_everything():
    score = evaluate_a1.score_case(case(["milk"]), {
        "status": "invalid", "fields": None, "extras": {}, "model_unsupported": [],
        "suggested": None}, None)
    assert not score["valid_json_rate"] and not score["allergens_captured"]
    assert not score["dish_resolution"]


def test_summary_rates_and_slices():
    records = []
    for avoid, model, unsupported in ((["milk"], ["milk"], ["halal"]), (["egg"], [], [])):
        c, r = case(avoid, unsupported=unsupported), result(model, unsupported=unsupported)
        records.append({**c, "result": r, "score": evaluate_a1.score_case(c, r, "pad-thai"),
                        "calls": 1, "latency_s": 1.0, "cached_calls": 0, "input_tokens": 5,
                        "output_tokens": 5})
    summary = evaluate_a1.summarize(records)
    assert summary["allergen_exact"] == 0.5 and summary["allergens_captured"] == 0.5
    assert summary["crosscheck_catch_rate"] == 0.0 and summary["model_missed"] == 1
    assert summary["unsupported_recall"] == 1.0 and summary["unsupported_cases"] == 1
    assert summary["slices"]["allergen"]["egg"]["captured_rate"] == 0.0
    assert summary["latency_mean_s"] == 1.0


def test_run_end_to_end_with_a_fake_model(pcfg, fake_server):
    write_a1(pcfg, config.load("evalsets"))
    check = CrossCheck(config.load("parse_request")["cross_check"])

    def perfect(prompt):  # answers with what the cross-check finds: a well-behaved model
        chat = prompt.rsplit("Chat:\n", 1)[1].split("\n\nAnswer with")[0]
        messages = [{"role": "user", "content": line.split(": ", 1)[1]}
                    for line in chat.splitlines() if line.startswith("Shopper: ")]
        scan = check.scan(messages)
        zip5 = next(w[:5] for m in messages for w in m["content"].replace("?", " ").split()
                    if w[:5].isdigit() and len(w) >= 5)
        return {"query": "pad thai", "zip": zip5, "avoid": scan.avoid, "diet": scan.diet,
                "extras": {"servings": None, "tweaks": [], "unsupported": scan.unsupported}}
    fake_server(perfect)
    summary = evaluate_a1.run("qwen3-4b", "dev", limit=10, use_mlflow=False, run_id="a1run")
    assert summary["cases"] == 10 and summary["valid_json_rate"] == 1.0
    assert summary["needs_confirmation_rate"] == 0.0
    saved = json.loads((config.REPORTS / "a1" / "a1run" / "summary.json").read_text())
    assert saved["params"]["prompt_version"] == "parse_request/1+rules_v2"  # the default


def test_the_draft_csv_becomes_a_dev_split(pcfg, tmp_path):
    from pantrypath_ml.evalsets import convert_a1_csv, held_out_dish_ids

    ecfg = config.load("evalsets")
    test_ids = held_out_dish_ids(CATALOG, ecfg["a1"]["seed"], ecfg["a1"]["test_fraction"])
    dev_name = next(d["name"] for d in CATALOG if d["dish_id"] not in test_ids)
    test_name = next(d["name"] for d in CATALOG if d["dish_id"] in test_ids)
    path = tmp_path / "draft.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "message_1", "message_2", "dish", "zip", "avoid", "diet",
                    "unsupported"])
        w.writerow(["ind-01", f"{dev_name} near 02115, no soy or milk", "and keep it kosher",
                    dev_name, "02115", "soy;milk", "none", "kosher"])
        w.writerow(["ind-02", f"{test_name} please", "", test_name, "", "", "none", ""])
        w.writerow(["ind-03", "mystery stew 02115", "", "mystery stew", "02115", "", "none", ""])
        w.writerow(["ind-04", f"{dev_name}, low sodium", "", dev_name, "", "", "vegan",
                    "low sodium"])
    out = convert_a1_csv(pcfg, ecfg, path)
    assert out["cases"] == 2 and [u["id"] for u in out["unresolved"]] == ["ind-03"]
    assert [t["id"] for t in out["test_dishes_left_out"]] == ["ind-02"]  # never a test dish
    cases = load_a1(ecfg, "realistic_dev")   # a dev split: no allow_test needed
    first = cases[0]
    assert first["expected"]["avoid"] == ["milk", "soy"]   # allergen order
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "user"]
    assert first["expected"]["unsupported"] == ["kosher"] and first["split"] == "realistic_dev"
    assert cases[1]["expected"]["unsupported"] == ["low_sodium"]


def test_realistic_dev_is_well_labelled():
    path = ML_HOME / "evalsets" / "a1_realistic_dev.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    allergens = set(CrossCheck(config.load("parse_request")["cross_check"]).category)
    labels = set(config.load("parse_request")["unsupported_labels"])
    dev_dishes = {json.loads(line)["dish_id"] for line in
                  (ML_HOME / "evalsets" / "a1_dev.jsonl").read_text().splitlines()}
    assert len(rows) >= 30 and len({r["case_id"] for r in rows}) == len(rows)
    for r in rows:
        e, text = r["expected"], " ".join(m["content"] for m in r["messages"])
        assert r["split"] == "realistic_dev" and r["dish_id"] in dev_dishes  # no test dish
        assert e["zip"] is None or e["zip"] in text
        assert set(e["avoid"]) <= allergens and set(e["unsupported"]) <= labels
        assert e["diet"] in ("none", "vegetarian", "vegan")

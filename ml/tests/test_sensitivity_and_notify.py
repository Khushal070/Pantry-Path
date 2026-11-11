"""Perturbations, setting curves, and notifications (no email or Slack is sent)."""

import random

from pantrypath_ml import config, notify, sensitivity

CASE = {"case_id": "c1", "dish_id": "pad-thai", "split": "dev",
        "messages": [{"role": "user", "content": "I'm allergic to peanuts. I would like to "
                      "prepare pad thai. I live in 02115."}],
        "expected": {"zip": "02115", "avoid": ["peanuts"], "diet": "none", "servings": None,
                     "unsupported": []},
        "slices": {"style": "formal", "allergens": ["peanuts"], "diet": "none",
                   "has_unsupported": False}}


def test_perturbations_keep_the_expected_answer():
    paraphrases = config.load("evalsets")["sensitivity"]["paraphrases"]
    for kind in sensitivity.PERTURBATIONS:
        out = sensitivity.perturb(CASE, kind, random.Random(1), paraphrases)
        assert out["expected"] == CASE["expected"] and out["slices"]["perturbation"] == kind
        assert out["case_id"] == f"c1~{kind}"
    late = sensitivity.perturb(CASE, "late", random.Random(1), paraphrases)
    assert late["messages"][0]["content"].endswith("allergic to peanuts.")
    para = sensitivity.perturb(CASE, "paraphrase", random.Random(1), paraphrases)
    assert "have an allergy to" in para["messages"][0]["content"]
    assert CASE["messages"][0]["content"].startswith("I'm allergic")  # original untouched


def test_perturbations_are_seeded():
    one = sensitivity.perturb(CASE, "reorder", random.Random(3), {})
    two = sensitivity.perturb(CASE, "reorder", random.Random(3), {})
    assert one == two


def test_curve_means_per_x_and_group():
    rows = [{"t": 0.0, "g": "a", "m": 0.5}, {"t": 0.0, "g": "a", "m": 1.0},
            {"t": 0.5, "g": "a", "m": None}, {"t": 0.5, "g": "b", "m": 0.2}]
    xs, series = sensitivity.curve(rows, lambda r: r["t"], lambda r: r["g"], "m")
    assert xs == [0.0, 0.5] and series == {"a": [0.75, None], "b": [None, 0.2]}


def test_notify_logs_without_secrets_and_posts_to_slack(monkeypatch):
    sent = []

    class Resp:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: sent.append(json)
                        or Resp())
    assert notify.notify("completed", "subject", "body") == {"emailed": False, "slack": False}
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    assert notify.notify("gate_failed", "gates", "recall 0.7")["slack"] is True
    assert sent[0]["text"].startswith("*WARNING* gates")


def test_a_broken_webhook_never_raises(monkeypatch):
    def boom(*a, **k):
        raise notify.requests.ConnectionError("down")
    monkeypatch.setattr(notify.requests, "post", boom)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    assert notify.post_slack("hi") is False

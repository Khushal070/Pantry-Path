"""Latency check against the deployed model server (A1 per request, A2 per dish, A3 per answer).

Runs a few A1, A2 and A3 dev items through the champion's settings against MODEL_SERVER_URL,
with a fresh cache so every call is real, and compares the mean latency with
gates.yaml `latency` (p95 is reported too; A3 has no limit there, so its latency is only
reported). A private Cloud Run service needs an identity token in MODEL_SERVER_TOKEN:

    export MODEL_SERVER_URL=https://<service-url>
    export MODEL_SERVER_TOKEN="$(gcloud auth print-identity-token)"
    python ml/serving/latency_check.py [--a1 10] [--a2 3] [--a3 3]
    python ml/serving/latency_check.py --smoke      # after a deploy (CI)

--smoke checks that the deployed server works (3 A1 chats, 1 A2 dish, 1 A3 answer, every
reply valid) and only reports latency; it exits 1 if a reply is unusable, so CI can roll
back. The default mode also fails when the mean latency is over gates.yaml `latency`.

The first request after scaling from zero includes the cold start; it is warmed up first
and not counted.
"""

import argparse
import json
import logging
import sys

from pantrypath_ml import config, evaluate_a1, evaluate_a2, evaluate_a3
from pantrypath_ml.local_llm import RecordingClient, cached_client, get_candidate, load_candidates
from pantrypath_ml.reporting import new_run_id
from pantrypath_ml.select import CHAMPION, read_record
from pantrypath_pipeline.config import secret
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger("latency_check")


def warm_up(candidate_name: str, quantization: str) -> None:
    ccfg = load_candidates()
    candidate = get_candidate(ccfg, candidate_name, quantization=quantization)
    client = RecordingClient(cached_client(ccfg, candidate, "warmup", run_id=new_run_id("warm")))
    client.complete_json('Reply with {"ok": true}.', {
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
        "additionalProperties": False}, purpose="warmup")
    log.info("Warm-up call took %.1fs (cold start included)", client.take()["latency_s"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--a1", type=int, default=10, help="A1 dev cases")
    parser.add_argument("--a2", type=int, default=3, help="A2 dev dishes")
    parser.add_argument("--a3", type=int, default=3, help="A3 dev cases")
    parser.add_argument("--smoke", action="store_true",
                        help="3 A1 + 1 A2 + 1 A3 items; fail only on unusable replies")
    args = parser.parse_args(argv)
    if args.smoke:
        args.a1, args.a2, args.a3 = 3, 1, 1
    setup_logging()
    if not secret("MODEL_SERVER_URL"):
        parser.error("set MODEL_SERVER_URL to the deployed server")
    champion = read_record(CHAMPION)
    if champion is None:
        parser.error(f"{CHAMPION} has no champion")
    limits = config.load("gates")["latency"]
    common = dict(server="ollama", quantization=champion["quantization"], use_mlflow=False,
                  tags={"stage": "latency_check"})
    warm_up(champion["candidate"], champion["quantization"])
    a1 = evaluate_a1.run(champion["candidate"], "dev", limit=args.a1,
                         variant=champion["a1"]["prompt_variant"],
                         overrides={k: champion["a1"][k] for k in ("temperature", "think")},
                         **common)
    a2 = evaluate_a2.run(champion["candidate"], "dev", limit=args.a2,
                         variant=champion["a2"].get("prompt_variant"),
                         overrides={k: champion["a2"][k] for k in ("temperature", "think")},
                         **common)
    a3 = evaluate_a3.run(champion["candidate"], "dev", limit=args.a3,
                         variant=champion["a3"]["prompt_variant"],
                         overrides={k: champion["a3"][k] for k in ("temperature", "think")},
                         **common)
    result = {
        "a1": {"mean_s": a1["latency_mean_s"], "p95_s": a1["latency_p95_s"],
               "max_s": limits["a1_max_s"]},
        "a2": {"mean_s": a2["latency_mean_s"], "p95_s": a2["latency_p95_s"],
               "max_s": limits["a2_max_s"]},
        "a3": {"mean_s": a3["latency_mean_s"], "p95_s": a3["latency_p95_s"],
               "max_s": limits.get("a3_max_s")},
    }
    result["a1"]["valid"] = a1["valid_json_rate"] == 1.0
    result["a2"]["valid"] = a2["valid_profile_rate"] == 1.0
    result["a3"]["valid"] = a3["valid_json_rate"] == 1.0
    for r in result.values():
        if r["max_s"] is None:  # no limit in gates.yaml: reported only
            r["latency_ok"] = None
            r["passed"] = r["valid"]
            continue
        r["latency_ok"] = r["mean_s"] is not None and r["mean_s"] <= r["max_s"]
        r["passed"] = r["valid"] and (args.smoke or r["latency_ok"])
    result["mode"] = "smoke" if args.smoke else "latency"
    print(json.dumps(result))
    return 0 if all(r["passed"] for k, r in result.items() if k != "mode") else 1


if __name__ == "__main__":
    sys.exit(main())

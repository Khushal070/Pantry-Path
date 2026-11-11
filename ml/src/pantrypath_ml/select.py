"""Gates, selection and the champion record.

    python -m pantrypath_ml.select choose --search-id ID [--write-proposed]
        settings that pass the validation and bias gates on dev; per model and quantization
        the best A1 and A2 setting; the smallest model wins, then the best score.
    python -m pantrypath_ml.select final [--proposed registry/proposed.json] [--ci]
        runs the proposed setting once on the A1 and A2 TEST sets (logged), applies the
        gates and writes reports/final/<id>/. --ci uses gates.yaml ci limits. Exit 1 on a
        failed gate.
    python -m pantrypath_ml.select promote --final reports/final/<id>/final.json
        replaces registry/champion.json only if the new setting is no worse on any gated
        metric and better on the score (a tie goes to the smaller model); the old record
        is kept as registry/champion.previous.json for rollback.
"""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config
from pantrypath_ml.reporting import write_json
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

REGISTRY = config.ML_HOME / "registry"
PROPOSED = REGISTRY / "proposed.json"
CHAMPION = REGISTRY / "champion.json"
PREVIOUS = REGISTRY / "champion.previous.json"


# ---------------------------------------------------------------- gates and score


def gate(task: str, summary: dict[str, Any], bias_gap: float | None,
         gates: Config) -> dict[str, Any]:
    """Validation targets and the bias gap for one task's summary."""
    checks, failures = {}, []
    for metric, rule in gates["validation"][task].items():
        value = summary.get(metric)
        ok = value is not None and value >= float(rule["min"])
        checks[metric] = {"value": value, "min": rule["min"], "passed": ok}
        if not ok:
            failures.append(f"{metric} {value} < {rule['min']}")
    max_gap = float(gates["bias"]["max_gap"])
    bias_ok = bias_gap is None or bias_gap <= max_gap  # None: no group big enough to compare
    if not bias_ok:
        failures.append(f"bias gap {bias_gap} > {max_gap}")
    return {"passed": not failures, "validation": checks,
            "bias": {"gap": bias_gap, "max": max_gap, "passed": bias_ok}, "failures": failures}


def score(task: str, metrics: dict[str, Any], gates: Config) -> float:
    return round(sum(float(w) * float(metrics.get(m) or 0)
                     for m, w in gates["score"][task].items()), 4)


# ---------------------------------------------------------------- choose


def choose(table: list[dict[str, Any]], gates: Config) -> dict[str, Any] | None:
    """Best passing A1 and A2 setting per (candidate, quantization); smallest model first."""
    options = []
    groups = {(r["candidate"], r["quantization"]) for r in table}
    for candidate, quant in sorted(groups):
        best = {}
        for task in ("a1", "a2"):
            passing = [r for r in table if r["task"] == task and r["passed"]
                       and (r["candidate"], r["quantization"]) == (candidate, quant)]
            if passing:
                best[task] = max(passing, key=lambda r, t=task: (score(t, r, gates),
                                                                 -r["latency_mean_s"]))
        if len(best) == 2:
            options.append((best["a1"]["params_b"],
                            -(score("a1", best["a1"], gates) + score("a2", best["a2"], gates)),
                            best))
    if not options:
        return None
    _, _, best = min(options, key=lambda o: (o[0], o[1]))
    a1, a2 = best["a1"], best["a2"]
    return {"candidate": a1["candidate"], "quantization": a1["quantization"],
            "server": a1["server"],
            "a1": {"prompt_variant": a1["prompt_variant"], "temperature": a1["temperature"],
                   "think": a1["think"], "dev_run": a1["run_id"]},
            "a2": {"temperature": a2["temperature"], "think": a2["think"],
                   "dev_run": a2["run_id"]}}


# ---------------------------------------------------------------- champion


def read_record(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()).get("champion") if path.is_file() else None


def gated_metrics(gates: Config) -> list[tuple[str, str]]:
    return [(t, m) for t, rules in gates["validation"].items() for m in rules]


def better(new: dict[str, Any], old: dict[str, Any] | None, kind: str,
           gates: Config) -> tuple[bool, str]:
    """Is `new` (metrics of `kind`: full | ci) good enough to replace the champion?"""
    if old is None:
        return True, "no champion yet"
    old_metrics = (old.get("metrics") or {}).get(kind)
    if not old_metrics:  # nothing comparable: never replace blind
        return False, f"the champion has no {kind} metrics to compare with"
    new_metrics = new["metrics"][kind]
    worse = [f"{t}.{m}" for t, m in gated_metrics(gates)
             if (new_metrics[t].get(m) or 0) < (old_metrics[t].get(m) or 0)]
    if worse:
        return False, "worse than the champion on " + ", ".join(worse)
    new_score = score("a1", new_metrics["a1"], gates) + score("a2", new_metrics["a2"], gates)
    old_score = score("a1", old_metrics["a1"], gates) + score("a2", old_metrics["a2"], gates)
    if new_score > old_score:
        return True, f"score {new_score:.4f} > {old_score:.4f}"
    if new_score == old_score and new["params_b"] < old["params_b"]:
        return True, "same score, smaller model"
    return False, f"score {new_score:.4f} is not above the champion's {old_score:.4f}"


def promote(final: dict[str, Any], kind: str, gates: Config, *,
            champion_path: Path = CHAMPION, previous_path: Path = PREVIOUS) -> dict[str, Any]:
    if not final["passed"]:
        return {"promoted": False, "reason": "the final run failed a gate"}
    old = read_record(champion_path)
    record = {**final["setting"], "params_b": final["params_b"], "models": final["models"],
              "prompt_versions": final["prompt_versions"],
              "metrics": {kind: final["metrics"]},
              "promoted_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "final_id": final["final_id"], "git_commit": final["git_commit"]}
    ok, reason = better(record, old, kind, gates)
    if old is not None and not (old.get("metrics") or {}).get(kind) and _same_setting(old, record):
        old.setdefault("metrics", {})[kind] = final["metrics"]
        write_json(champion_path, {"champion": old})
        return {"promoted": False, "reason": f"same setting: recorded its {kind} metrics as "
                                             "the champion's baseline"}
    if ok:
        if old is not None:
            write_json(previous_path, {"champion": old})
        write_json(champion_path, {"champion": record})
    log.info("Champion %s: %s", "replaced" if ok else "kept", reason)
    return {"promoted": ok, "reason": reason}


def _same_setting(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("candidate", "quantization", "a1", "a2"))


# ---------------------------------------------------------------- final confirmation


def final(proposed: dict[str, Any], *, ci: bool = False, use_mlflow: bool = True,
          server: str | None = None) -> dict[str, Any]:
    """The proposed setting on the A1 and A2 test sets, then the gates.

    Runs on the default server (Ollama, what is deployed) unless `server` is given, even
    when the setting was chosen on vLLM.
    """
    from pantrypath_ml import bias, evaluate_a1, evaluate_a2
    from pantrypath_ml.local_llm import get_candidate, load_candidates
    from pantrypath_ml.registry import git_commit

    gates = config.load("gates")
    final_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{'ci' if ci else 'final'}"
    limits = gates["ci"]["limit"] if ci else {"a1": None, "a2": None}
    ccfg = load_candidates()
    server = server or ccfg["default_server"]
    tags = {"final_id": final_id, "stage": "ci" if ci else "final"}
    common = dict(server=server, quantization=proposed["quantization"],
                  use_mlflow=use_mlflow, tags=tags)
    log.warning("Final confirmation %s: reading the held-out TEST splits", final_id)
    a1 = evaluate_a1.run(proposed["candidate"], "test", variant=proposed["a1"]["prompt_variant"],
                         limit=limits["a1"], run_id=f"{final_id}-a1",
                         overrides={k: proposed["a1"][k] for k in ("temperature", "think")},
                         **common)
    a2 = evaluate_a2.run(proposed["candidate"], "test", limit=limits["a2"],
                         run_id=f"{final_id}-a2",
                         overrides={k: proposed["a2"][k] for k in ("temperature", "think")},
                         **common)
    report = bias.run(config.REPORTS / "a1" / a1["run_id"], config.REPORTS / "a2" / a2["run_id"],
                      out=config.REPORTS / "final" / final_id / "bias")
    main = gates["bias"]["main_metric"]
    verdicts = {t: gate(t, s, report[t]["largest_gap"][main[t]]["gap"], gates)
                for t, s in (("a1", a1), ("a2", a2))}
    candidate = get_candidate(ccfg, proposed["candidate"], server=server,
                              quantization=proposed["quantization"])
    keep = {"a1": ["cases", *gates["validation"]["a1"], *gates["score"]["a1"], "latency_mean_s",
                   "latency_p95_s"],
            "a2": ["cases", *gates["validation"]["a2"], *gates["score"]["a2"], "latency_mean_s",
                   "latency_p95_s"]}
    result = {
        "final_id": final_id, "kind": "ci" if ci else "full",
        "setting": {k: proposed[k] for k in ("candidate", "quantization", "a1", "a2")},
        "params_b": candidate.params_b,
        "models": ccfg["candidates"][proposed["candidate"]]["quantizations"][
            proposed["quantization"]],
        "prompt_versions": {"a1": a1["params"]["prompt_version"],
                            "a2": a2["params"]["prompt_version"]},
        "metrics": {t: {k: s.get(k) for k in keep[t]} for t, s in (("a1", a1), ("a2", a2))},
        "gates": verdicts, "passed": all(v["passed"] for v in verdicts.values()),
        "runs": {"a1": a1["run_id"], "a2": a2["run_id"]}, "git_commit": git_commit(),
    }
    folder = config.REPORTS / "final" / final_id
    write_json(folder / "final.json", result)
    (folder / "final.md").write_text(render_final(result))
    log.info("Final %s: %s", final_id, "PASSED" if result["passed"] else "FAILED gates")
    return result


def render_final(result: dict[str, Any]) -> str:
    from pantrypath_ml.charts import markdown_table, pct

    lines = [f"# Final confirmation {result['final_id']} ({result['kind']})", "",
             f"Setting: `{json.dumps(result['setting'])}`", "",
             f"**{'Passed' if result['passed'] else 'Failed'}** all gates.", ""]
    for task, verdict in result["gates"].items():
        rows = [[m, pct(c["value"]), pct(float(c["min"])), "yes" if c["passed"] else "NO"]
                for m, c in verdict["validation"].items()]
        rows.append(["bias gap (max)", pct(verdict["bias"]["gap"]),
                     pct(verdict["bias"]["max"]), "yes" if verdict["bias"]["passed"] else "NO"])
        lines += [f"## {task.upper()} (test)", "",
                  markdown_table(["Metric", "Value", "Target", "Met"], rows), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.select",
                                     description="Gates, selection and the champion record")
    parser.add_argument("command", choices=["choose", "final", "promote"])
    parser.add_argument("--search-id", help="choose: the search to choose from")
    parser.add_argument("--write-proposed", action="store_true",
                        help="choose: also write registry/proposed.json")
    parser.add_argument("--proposed", type=Path, default=PROPOSED)
    parser.add_argument("--ci", action="store_true", help="final: the small CI subset")
    parser.add_argument("--server", help="final: override the proposed server")
    parser.add_argument("--no-mlflow", action="store_true")
    parser.add_argument("--final", type=Path, help="promote: a final.json")
    args = parser.parse_args(argv)
    setup_logging()
    gates = config.load("gates")
    try:
        if args.command == "choose":
            if not args.search_id:
                parser.error("choose needs --search-id")
            path = config.REPORTS / "search" / args.search_id / "comparison.json"
            chosen = choose(json.loads(path.read_text())["rows"], gates)
            if chosen is None:
                log.error("No setting passes every gate in search %s", args.search_id)
                return 1
            chosen["source"] = f"search {args.search_id}"
            write_json(config.REPORTS / "selection" / args.search_id / "chosen.json", chosen)
            if args.write_proposed:
                write_json(args.proposed, chosen)
            print(json.dumps(chosen))
            return 0
        if args.command == "final":
            result = final(json.loads(args.proposed.read_text()), ci=args.ci,
                           use_mlflow=not args.no_mlflow, server=args.server)
            print(json.dumps({k: result[k] for k in ("final_id", "passed", "metrics")}))
            return 0 if result["passed"] else 1
        if not args.final:
            parser.error("promote needs --final")
        result_file = json.loads(args.final.read_text())
        decision = promote(result_file, result_file["kind"], gates)
        print(json.dumps(decision))
        return 0
    except PipelineError as exc:
        log.error("select %s failed: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

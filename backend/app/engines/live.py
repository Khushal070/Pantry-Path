"""Live engine: the champion's A1, A2 and A3 and the data pipeline's request flow.

Needs requirements-live.txt, the dish catalog and allergen snapshot (dvc pull), a model
server (MODEL_SERVER_URL, or the Ollama default in ml/configs/candidates.yaml) and the
Kroger keys in the repo-root .env. See backend/README.md.

The models never decide what is safe: verdicts come from diet_rules inside build_result,
and A3's answer is held to result.json by its own check before it gets here.

A search with stronger=True works out an unseen dish's ingredients with the stronger
model (stronger_model.py: the pipeline's own LLM client from .env) instead of the
champion's A2. Everything else is the same.
"""

import logging
import re
import string
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.engines.base import EngineError, Issue, ParseOutcome, Progress, SearchOutcome
from app.stronger_model import detect, error_for
from app.vocab import DIET_RANK

log = logging.getLogger(__name__)
PLAN_PROMPT = Path(__file__).resolve().parents[3] / "ml" / "prompts" / "plan_dish.txt"
ZIP_CONFLICT = "the chat gives ZIP"   # how A1 words a ZIP conflict (parse_request.py)


def no_stores_message(zip_code: str, radius_miles: float, providers: list[str]) -> str:
    """Why a search came back without stores, and what to do about it."""
    chains = "Kroger-family or Target store" if "target" in providers else "Kroger-family store"
    message = (f"There's no {chains} within {radius_miles:g} miles of {zip_code}, so there are "
               "no products to check. Kroger's stores (Kroger, Ralphs, Fred Meyer, King Soopers, "
               "Harris Teeter and others) aren't in every region. Ask again with a ZIP where "
               "they are, such as 45202, or change your ZIP in Preferences.")
    if "target" not in providers:
        message += " (The server can also search Target stores, with a RedCircle key.)"
    return message


def outcome_from(parsed: Any, in_catalog: bool | None) -> ParseOutcome:
    """A1's ParseResult as the chat's ParseOutcome. For needs_confirmation the request is
    A1's stricter suggestion; the issues say why, from A1's own structured fields."""
    if parsed.status == "invalid" or not parsed.fields:
        return ParseOutcome(status="invalid", error=parsed.error)
    fields, scan = parsed.fields, parsed.scan or {}
    if parsed.status == "ok":
        base = parsed.request
    elif parsed.status == "needs_confirmation":
        base = parsed.suggested
    else:   # incomplete: no ZIP yet
        base = {**fields, "zip": None}
    issues = [Issue("missed_allergen", allergen=a) for a in scan.get("avoid", [])
              if a not in fields["avoid"]]
    if DIET_RANK[scan.get("diet", "none")] > DIET_RANK[fields["diet"]]:
        issues.append(Issue("diet", value=scan["diet"]))
    issues += [Issue("clarify", allergen=a) for a in parsed.clarify]
    issues += [Issue("unmentioned", allergen=a) for a in parsed.unmentioned]
    issues += [Issue("zip", value=base.get("zip")) for c in parsed.conflicts
               if c.startswith(ZIP_CONFLICT)]
    if len(issues) != len(parsed.conflicts):
        # A conflict this code doesn't know how to read: show it as it is, never drop it
        log.warning("Unclassified A1 conflicts: %s", parsed.conflicts)
        issues.append(Issue("other", value="; ".join(parsed.conflicts)))
    return ParseOutcome(
        status="ok", query=base.get("query"), zip=base.get("zip"),
        avoid=list(base.get("avoid") or []), diet=base.get("diet") or "none",
        extras=dict(parsed.extras or {}), issues=issues,
        mentioned=list(scan.get("mentioned", [])), chat_avoid=list(scan.get("avoid", [])),
        clarify=list(parsed.clarify), in_catalog=in_catalog, notes=list(parsed.notes),
    )


class LiveEngine:
    mode = "live"

    def __init__(self) -> None:
        try:
            from pantrypath_ml import runtime
            from pantrypath_pipeline.config import load_config
            from pantrypath_pipeline.errors import ProviderError
            from pantrypath_pipeline.providers.base import make_providers
        except ImportError as exc:
            raise RuntimeError("Live mode needs the model and pipeline packages: "
                               "pip install -r requirements-live.txt (from backend/)") from exc
        self.cfg = load_config()
        self.models = runtime.load()
        self.stronger = detect(self.cfg)
        if not self.stronger.available:
            log.info("No stronger model: %s", self.stronger.reason)
        # The store providers once, for the process: Kroger's token and its 15-minute
        # in-memory cache then carry over between searches (build_result would otherwise
        # make new ones every time). Without their keys the chat still works and searches
        # say what's missing; adding keys to .env needs a restart.
        self.providers: list[Any] | None = None
        self.store_problem: str | None = None
        try:
            self.providers = make_providers(self.cfg)
        except ProviderError as exc:
            self.store_problem = str(exc)
            log.warning("The store search is off until this is fixed: %s", exc)
        self._screen: Any = None
        # Background product searches for guided builds (prefetch), by (zip, term)
        self._prefetch_pool = ThreadPoolExecutor(2, thread_name_prefix="prefetch")
        self._prefetch_lock = threading.Lock()
        self._inflight: dict[tuple[str, str], Future] = {}
        log.info("Live engine ready: %s", self.info())

    @property
    def screen(self) -> Any:
        if self._screen is None:
            from app.screen import Screen
            self._screen = Screen()
        return self._screen

    def info(self) -> dict[str, Any]:
        setting = self.models.setting
        return {"mode": self.mode, "candidate": setting.get("candidate"),
                "quantization": setting.get("quantization"),
                "model": setting.get("models"), "prompt_versions": self.models.versions,
                "a3_available": self.models.a3_available,
                "stores": [p.name for p in self.providers or []],
                "store_problem": self.store_problem,
                "stronger_model": self.stronger.view(),
                "stronger_problem": self.stronger.reason}

    def parse(self, messages: list[dict[str, str]]) -> ParseOutcome:
        parsed = self.models.parse(messages)
        query = (parsed.request or parsed.suggested or parsed.fields or {}).get("query")
        in_catalog = None
        if query and self.models.resolve is not None:
            try:
                in_catalog = self.models.resolve(query) is not None
            except Exception:   # the lookup is a nicety on the card; never block the chat
                log.exception("Catalog lookup failed for %r", query)
        return outcome_from(parsed, in_catalog)

    def search(self, request: dict[str, Any], extras: dict[str, Any], clarify: list[str],
               progress: Progress, *, stronger: bool = False) -> SearchOutcome:
        from pantrypath_pipeline.request import build_result

        self._check_stores()
        if stronger and not self.stronger.available:
            raise EngineError("No stronger model is set up on this server "
                              f"({self.stronger.reason}).", kind="stronger_unavailable")
        progress("searching")
        # stronger: no client, so build_result uses the pipeline's .env client (with its
        # own reply cache); the champion's A2 prompt either way
        result = self._run(lambda: build_result(
            self.cfg, request["query"], request["zip"], request["avoid"], request["diet"],
            providers=self.providers,
            llm_client=None if stronger else self.models.a2_client,
            llm_template=self.models.a2_template), request, stronger=stronger)
        payload = result.model_dump(mode="json")
        generated = payload["dish"]["source"] != "catalog"   # A2 worked out the ingredients
        a2 = None
        if generated:
            a2 = self.stronger.label if stronger else self.models.setting.get("candidate")
        return self._finish(result, request, extras, clarify, progress,
                            {"a2": a2, "stronger": stronger and generated})

    # ------------------------------------------------------------ guided builds

    def plan(self, query: str, avoid: list[str], diet: str) -> Any:
        """A template, else the catalog, else one call to the champion's model with the
        plan prompt (ml/prompts/plan_dish.txt; replies cached like A2's)."""
        from app.plans import PLAN_SCHEMA, PlanError, build_plan
        from pantrypath_pipeline.errors import LLMError
        from pantrypath_pipeline.request import catalog_lookup

        db_path = self.cfg.path(self.cfg["catalog"]["sqlite_output"])

        def catalog(text: str) -> tuple[str, str, list[dict[str, Any]]] | None:
            if not db_path.is_file():
                return None
            profile = catalog_lookup(db_path, text, self.cfg["request"]["fuzzy_min_score"])
            if profile is None:
                return None
            return (profile.name, getattr(profile.cuisine, "value", profile.cuisine),
                    [i.model_dump(mode="json") for i in profile.ingredients])

        def model(text: str) -> dict[str, Any]:
            if not PLAN_PROMPT.is_file():   # the prompt ships with ml/ (its own PR)
                raise PlanError(f"no plan prompt at {PLAN_PROMPT}")
            prompt = string.Template(PLAN_PROMPT.read_text()).safe_substitute(dish=text)
            reply = self.models.a2_client.complete_json(prompt, PLAN_SCHEMA,
                                                        purpose=f"plan_dish:{text}")
            return reply.data

        try:
            return build_plan(query, avoid, diet, self.screen, catalog=catalog, model=model)
        except (PlanError, LLMError) as exc:
            log.warning("No plan for %r: %s", query, exc)
            raise EngineError(f"I couldn't plan the choices for \"{query}\".",
                              kind="plan_failed") from exc

    def prefetch(self, zip_code: str, terms: list[str]) -> None:
        """Warm the Kroger provider's 15-minute cache: the stores near the ZIP (no terms),
        or products for these terms at them. Kroger only: Target (RedCircle) costs a
        credit per call, so it's used only by the final search, for what Kroger lacks."""
        from pantrypath_pipeline.request import gather_products

        kroger = [p for p in self.providers or [] if p.name == "kroger"]
        if not kroger:
            return
        settings = self.cfg["request"]
        with self._prefetch_lock:
            if not terms:
                key = (zip_code, "")
                if key not in self._inflight:
                    self._inflight[key] = self._prefetch_pool.submit(
                        kroger[0].find_stores, zip_code, settings["radius_miles"],
                        settings["max_stores"])
                return
            todo = [t for t in dict.fromkeys(terms) if (zip_code, t) not in self._inflight]
            if not todo:
                return
            future = self._prefetch_pool.submit(gather_products, self.cfg, kroger, todo,
                                                zip_code)
            for term in todo:
                self._inflight[(zip_code, term)] = future
        log.info("Prefetching %d term(s) near %s", len(todo), zip_code)

    def _wait_for_prefetch(self, zip_code: str, terms: list[str], timeout_s: float = 60) -> None:
        """Let running prefetches of these terms finish, so the final search reads them
        from the cache instead of asking Kroger again. Failures are ignored: the final
        search fetches what's missing."""
        with self._prefetch_lock:
            futures = {self._inflight.get((zip_code, t)) for t in [""] + terms} - {None}
            # forget finished ones; the provider's cache keeps their results for 15 minutes
            for key in [k for k, f in self._inflight.items() if f.done()]:
                del self._inflight[key]
        wait(futures, timeout=timeout_s)

    def options(self, request: dict[str, Any], terms: list[str]) -> dict[str, Any]:
        """Products for every option of a build at Kroger, judged (memory only). Reads what
        prefetch already fetched from the provider's cache."""
        from pantrypath_core.result import DishInfo, RequestInfo
        from pantrypath_pipeline.request import result_for_ingredients

        self._check_stores()
        kroger = [p for p in self.providers or [] if p.name == "kroger"]
        if not kroger:   # Target costs a credit per call: never for a build's options
            raise EngineError("Products are found when you search (no Kroger keys on this "
                              "server).", kind="config")
        ingredients = [SimpleNamespace(name=t, role="core", category="other")
                       for t in dict.fromkeys(terms)]
        self._wait_for_prefetch(request["zip"], [i.name for i in ingredients])
        result = self._run(lambda: result_for_ingredients(
            self.cfg, RequestInfo(**request),
            DishInfo(name=request["query"], cuisine="american", source="llm"),
            ingredients, providers=kroger, catalog_version="options"), request)
        return result.model_dump(mode="json")

    def search_ingredients(self, request: dict[str, Any], guided: dict[str, Any],
                           extras: dict[str, Any], clarify: list[str],
                           progress: Progress, *, answer: bool = True) -> SearchOutcome:
        from pantrypath_core.result import DishInfo, RequestInfo, Summary
        from pantrypath_pipeline.request import result_for_ingredients

        self._check_stores()
        progress("searching")
        # searched by "term" when a brand search found the pick; reported by item name
        ingredients = [SimpleNamespace(name=i.get("term") or i["name"], role=i["role"],
                                       category=i["category"]) for i in guided["ingredients"]]
        self._wait_for_prefetch(request["zip"], [i.name for i in ingredients])
        source = "catalog" if guided.get("source") == "catalog" else "llm"
        result = self._run(lambda: result_for_ingredients(
            self.cfg, RequestInfo(**request),
            DishInfo(name=guided["dish"], cuisine=guided.get("cuisine") or "american",
                     source=source),
            ingredients, providers=self.providers,
            catalog_version=f"guided-{guided.get('source')}"), request)
        for found, item in zip(result.ingredients, guided["ingredients"], strict=True):
            found.name = item["name"]
        result.summary = Summary.from_ingredients(result.ingredients)
        # the shopper chose the ingredients: nothing for a stronger model to redo
        return self._finish(result, request, extras, clarify, progress,
                            {"a2": None, "stronger": False}, answer=answer, guided=guided)

    # ------------------------------------------------------------ shared

    def _check_stores(self) -> None:
        if self.providers is None:
            raise EngineError("The store search isn't set up on this server yet: "
                              f"{self.store_problem}", kind="config")

    def _run(self, search: Any, request: dict[str, Any], *, stronger: bool = False) -> Any:
        """Run a pipeline search, turning its errors into messages for the shopper."""
        from pydantic import ValidationError

        from pantrypath_pipeline.errors import (
            DataUnavailableError,
            LLMError,
            PipelineError,
            ProviderError,
        )

        try:
            return search()
        except ValidationError as exc:
            raise EngineError(f"That request isn't valid: {exc.errors()[0]['msg']}",
                              kind="invalid") from exc
        except ProviderError as exc:
            log.warning("Store search failed: %s", type(exc).__name__)
            raise EngineError("The store search didn't answer. Please try again in a minute.",
                              kind="provider") from exc
        except DataUnavailableError as exc:
            raise EngineError("The server is missing its reference data (dish catalog or "
                              "allergen snapshot): run dvc pull.", kind="data") from exc
        except LLMError as exc:
            if stronger:
                raise error_for(exc, self.stronger.label or "The stronger model") from exc
            raise EngineError(f"I couldn't work out the ingredients of \"{request['query']}\". "
                              "Try another dish, or name it differently.",
                              kind="not_found") from exc
        except PipelineError as exc:
            raise EngineError(str(exc), kind="error") from exc

    def _finish(self, result: Any, request: dict[str, Any], extras: dict[str, Any],
                clarify: list[str], progress: Progress,
                model_used: dict[str, Any], answer: bool = True,
                guided: dict[str, Any] | None = None) -> SearchOutcome:
        """No stores: say why. Else A3's answer, or the store results without one."""
        from pantrypath_ml.parse_request import ParseResult
        from pantrypath_pipeline.errors import PipelineError

        if not result.stores:
            # Nothing was found to check or buy: say why, rather than an empty result and a
            # substitutes-only list from A3
            raise EngineError(no_stores_message(request["zip"],
                                                self.cfg["request"]["radius_miles"],
                                                [p.name for p in self.providers]),
                              kind="no_stores")
        payload = result.model_dump(mode="json")
        if not answer:
            return SearchOutcome(result=payload, answer_status="skipped",
                                 model_used=model_used)
        if not self.models.a3_available:
            return SearchOutcome(result=payload, answer_status="unavailable",
                                 model_used=model_used)
        progress("answering")
        chosen = (guided or {}).get("choices") or {}
        if chosen:
            from app.choices import narrow
            a3_payload = narrow(payload, chosen)   # A3 buys exactly the chosen product
        else:
            a3_payload = payload
        parsed = ParseResult("ok", request=request, extras=extras, clarify=clarify)
        try:
            reply = self.models.answer(a3_payload, parsed)
            if guided:
                reply = self._matching_steps(reply, a3_payload, request, extras, clarify,
                                             guided)
            text = reply.answer
        except PipelineError:   # LLMError included: show the store results without it
            log.exception("A3 failed; returning result.json without an answer")
            return SearchOutcome(result=payload, answer_status="failed", warnings=[
                "The shopping list writer didn't answer, so here are the store results as "
                "they are."], model_used=model_used, choices=chosen or None)
        return SearchOutcome(result=payload, answer=text, answer_status=reply.status,
                             warnings=list(reply.warnings), model_used=model_used,
                             choices=chosen or None)

    def food_vocabulary(self) -> set[str]:
        """Every catalog ingredient and every template or variant food (stepcheck.py)."""
        if getattr(self, "_vocabulary", None) is None:
            import json

            from app.plans import known_foods
            names = known_foods()
            path = self.cfg.path(self.cfg["catalog"]["profiles_output"])
            try:
                data = json.loads(path.read_text())
                dishes = data["dishes"] if isinstance(data, dict) else data
                names |= {i["name"] for d in dishes for i in d["ingredients"]}
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("No catalog for the steps check at %s", path)
            self._vocabulary = names
        return self._vocabulary

    def _matching_steps(self, reply: Any, payload: dict[str, Any],
                        request: dict[str, Any], extras: dict[str, Any], clarify: list[str],
                        guided: dict[str, Any]) -> Any:
        """Steps and summary that use only the final list: A3 once more if they don't
        (its reply, status and warnings then replace the first's), then repaired
        (stepcheck.py)."""
        text = reply.answer
        from app.stepcheck import correction, repair, strays
        from pantrypath_ml.parse_request import ParseResult

        allowed = [i["name"] for i in guided["ingredients"]]
        allowed += [i["substitute"] for i in text.get("ingredients") or []
                    if i.get("substitute")]
        vocabulary = self.food_vocabulary() | set(guided.get("offered") or [])
        found = {s for t in [text.get("summary") or "", *(text.get("steps") or [])]
                 for s in strays(t, allowed, vocabulary)}
        if found:
            log.info("A3's steps name %s, not on the list: asking again", sorted(found))
            tweaks = [*(extras.get("tweaks") or []), correction(allowed, found)]
            again = ParseResult("ok", request=request, extras={**extras, "tweaks": tweaks},
                                clarify=clarify)
            reply = self.models.answer(payload, again)
            text = reply.answer
            allowed += [i["substitute"] for i in text.get("ingredients") or []
                        if i.get("substitute")]
        replacements = guided.get("replacements") or {}
        summary = " ".join(repair(re.split(r"(?<=[.!?])\s+", text.get("summary") or ""),
                                  allowed, vocabulary, replacements))
        fixed = {**text, "summary": summary,
                 "steps": repair(list(text.get("steps") or []), allowed, vocabulary,
                                 replacements)}
        return SimpleNamespace(answer=fixed, status=reply.status,
                               warnings=list(reply.warnings))

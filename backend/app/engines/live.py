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
from typing import Any

from app.engines.base import EngineError, Issue, ParseOutcome, Progress, SearchOutcome
from app.stronger_model import detect, error_for
from app.vocab import DIET_RANK

log = logging.getLogger(__name__)
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
        log.info("Live engine ready: %s", self.info())

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
        from pydantic import ValidationError

        from pantrypath_ml.parse_request import ParseResult
        from pantrypath_pipeline.errors import (
            DataUnavailableError,
            LLMError,
            PipelineError,
            ProviderError,
        )
        from pantrypath_pipeline.request import build_result

        if self.providers is None:
            raise EngineError("The store search isn't set up on this server yet: "
                              f"{self.store_problem}", kind="config")
        if stronger and not self.stronger.available:
            raise EngineError("No stronger model is set up on this server "
                              f"({self.stronger.reason}).", kind="stronger_unavailable")
        progress("searching")
        try:
            # stronger: no client, so build_result uses the pipeline's .env client (with its
            # own reply cache); the champion's A2 prompt either way
            result = build_result(self.cfg, request["query"], request["zip"], request["avoid"],
                                  request["diet"], providers=self.providers,
                                  llm_client=None if stronger else self.models.a2_client,
                                  llm_template=self.models.a2_template)
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
        if not result.stores:
            # Nothing was found to check or buy: say why, rather than an empty result and a
            # substitutes-only list from A3
            raise EngineError(no_stores_message(request["zip"],
                                                self.cfg["request"]["radius_miles"],
                                                [p.name for p in self.providers]),
                              kind="no_stores")
        payload = result.model_dump(mode="json")
        generated = payload["dish"]["source"] != "catalog"   # A2 worked out the ingredients
        a2 = None
        if generated:
            a2 = self.stronger.label if stronger else self.models.setting.get("candidate")
        model_used = {"a2": a2, "stronger": stronger and generated}
        if not self.models.a3_available:
            return SearchOutcome(result=payload, answer_status="unavailable",
                                 model_used=model_used)
        progress("answering")
        parsed = ParseResult("ok", request=request, extras=extras, clarify=clarify)
        try:
            reply = self.models.answer(payload, parsed)
        except PipelineError:   # LLMError included: show the store results without it
            log.exception("A3 failed; returning result.json without an answer")
            return SearchOutcome(result=payload, answer_status="failed", warnings=[
                "The shopping list writer didn't answer, so here are the store results as "
                "they are."], model_used=model_used)
        return SearchOutcome(result=payload, answer=reply.answer, answer_status=reply.status,
                             warnings=list(reply.warnings), model_used=model_used)

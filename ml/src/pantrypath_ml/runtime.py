"""The champion at run time: what the backend imports to use A1, A2 and A3.

registry/champion.json records the chosen setting (model, quantization, the A1 and A2
prompt variants, temperature and thinking for each task). load() turns it into ready
clients and parse / answer functions, so the backend never re-assembles prompts or
settings by hand:

    from pantrypath_ml import runtime

    models = runtime.load()                      # once, at startup
    parsed = models.parse(messages)              # A1 + cross-check + ZIP check
    if parsed.status == "ok":
        request = parsed.request                 # RequestInfo as JSON
        # A2 runs inside the data pipeline when the dish is not in the catalog, with the
        # champion's A2 client and prompt:
        result = build_result(cfg, ..., llm_client=models.a2_client,
                              llm_template=models.a2_template)
        if models.a3_available:   # False when A3 is evaluated, not promoted
            reply = models.answer(result.model_dump(mode="json"), parsed)   # A3
            # reply.answer: shopping list, substitutes, quantities, steps (always passes
            # the grounding check); reply.warnings: lines the app must show with it
        # else: show result.json as it is
    elif parsed.status == "needs_confirmation":
        ...                                      # show parsed.conflicts / parsed.suggested
    elif parsed.status == "incomplete":
        ...                                      # ask for parsed.missing (the ZIP)
    # parsed.clarify lists allergens worth asking about ("nuts": peanuts too?)

The model server comes from MODEL_SERVER_URL (the Cloud Run URL in production) and the
default server in configs/candidates.yaml (Ollama, what is deployed). On Cloud Run set
MODEL_SERVER_AUTH=google so the backend mints its own ID tokens for the private service.
A1 and A2 replies are cached per setting under cache/llm/, like the evaluations. A3 is
never cached: its prompt holds Kroger product names, and Kroger's terms forbid keeping
them.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from pantrypath_ml import config, evaluate_a2, local_llm
from pantrypath_ml.answer import AnswerResult, SubstituteCheck, SubstituteTable, answer, load_table
from pantrypath_ml.answer import load_prompt as load_answer_prompt
from pantrypath_ml.local_llm import (
    cached_client,
    client_settings,
    get_candidate,
    load_candidates,
)
from pantrypath_ml.parse_request import (
    CrossCheck,
    ParseResult,
    catalog_resolver,
    load_prompt,
    parse_request,
)
from pantrypath_ml.select import CHAMPION, PROPOSED, a3_promoted, a3_setting
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.llm import LLMClient

log = logging.getLogger(__name__)


@dataclass
class Models:
    """A1, A2 and A3 configured from one champion record."""

    setting: dict[str, Any]
    a1_client: LLMClient
    a2_client: LLMClient
    a3_client: LLMClient | None    # None: A3 is evaluated, not promoted
    prcfg: Config
    acfg: Config
    checker: CrossCheck
    screen: SubstituteCheck
    table: SubstituteTable
    template: Template
    answer_template: Template
    variant: str
    a3_variant: str = "hybrid"
    # the champion's A2 prompt for build_result(llm_template=...); None = the pipeline's
    # own refine_dish prompt
    a2_template: evaluate_a2.Template | None = None
    versions: dict[str, str] = field(default_factory=dict)
    # the catalog lookup build_result() makes: A1 cleans a dish query it doesn't know
    resolve: Callable[[str | None], str | None] | None = None

    @property
    def a3_available(self) -> bool:
        return self.a3_client is not None

    def parse(self, messages: list[dict[str, str]]) -> ParseResult:
        """A1 on one chat: the model, then the deterministic cross-check and ZIP check."""
        return parse_request(self.prcfg, messages, self.a1_client, variant=self.variant,
                             template=self.template, checker=self.checker,
                             resolve=self.resolve)

    def answer(self, result: dict[str, Any], parsed: ParseResult | None = None) -> AnswerResult:
        """A3 on one result.json (as JSON), with A1's extras and clarify questions."""
        if self.a3_client is None:
            raise PipelineError("A3 is evaluated, not promoted, in this champion: show "
                                "result.json without an A3 answer (check a3_available)")
        return answer(self.acfg, result, self.a3_client,
                      extras=parsed.extras if parsed else None,
                      clarify=parsed.clarify if parsed else None, screen=self.screen,
                      template=self.answer_template, table=self.table,
                      variant=self.a3_variant)


def read_setting(path: Path = CHAMPION, *, allow_proposed: bool = False) -> dict[str, Any]:
    """The champion record, or with allow_proposed the proposed setting when there is no
    champion yet (for local development only: it has not passed the final gates)."""
    record = None
    if path.is_file():
        record = json.loads(path.read_text()).get("champion")
    if record:
        return record
    if allow_proposed and PROPOSED.is_file():
        log.warning("No champion in %s; using the proposed setting %s (not confirmed on "
                    "the held-out sets)", path, PROPOSED)
        return json.loads(PROPOSED.read_text())
    raise PipelineError(f"{path} has no champion; run select final and select promote first "
                        "(or pass allow_proposed=True for local development)")


def load(path: Path = CHAMPION, *, allow_proposed: bool = False,
         server: str | None = None) -> Models:
    """The clients (each task can use its own temperature and thinking), the champion's A2
    prompt, the parser and the answer."""
    setting = read_setting(path, allow_proposed=allow_proposed)
    ccfg = load_candidates()
    candidate = get_candidate(ccfg, setting["candidate"], server=server,
                              quantization=setting["quantization"])
    prcfg = config.load("parse_request")
    template, a1_file_version = load_prompt(prcfg)
    variant = setting["a1"]["prompt_variant"]
    a1_version = f"{a1_file_version}+{variant}"
    a2_template, a2_version = evaluate_a2.load_variant(
        config.pipeline_config(),
        setting["a2"].get("prompt_variant") or config.load("refine_dish")["default"])
    acfg = config.load("answer")
    a3_variant = a3_setting(setting).get("prompt_variant") or acfg["default_variant"]
    answer_template, a3_version = load_answer_prompt(acfg, a3_variant)
    expected = setting.get("prompt_versions") or {}
    for task, version in (("a1", a1_version), ("a2", a2_version), ("a3", a3_version)):
        if expected.get(task) and expected[task] != version:
            log.warning("The champion was confirmed with %s prompt %s, but the code now has "
                        "%s: rerun select final before relying on its numbers", task,
                        expected[task], version)

    def client(task: str, version: str) -> LLMClient:
        settings = client_settings(ccfg, {k: setting[task][k] for k in ("temperature", "think")},
                                   task=task)
        return cached_client(ccfg, candidate, version, settings=settings)

    a1_client, a2_client = client("a1", a1_version), client("a2", a2_version)
    # A3: no cache (Kroger product names are in the prompt); none when not promoted
    a3_client = None
    if a3_promoted(setting):
        a3 = a3_setting(setting)
        a3_client = local_llm.make_client(ccfg, candidate, client_settings(
            ccfg, {k: a3[k] for k in ("temperature", "think")}, task="a3"))
    else:
        log.info("A3 is evaluated, not promoted: models.answer is not available")
    log.info("Models: %s (%s) on %s", candidate.model, candidate.quantization, candidate.server)
    models = Models(setting=setting, a1_client=a1_client, a2_client=a2_client,
                    a3_client=a3_client, prcfg=prcfg,
                    acfg=acfg, checker=CrossCheck(prcfg["cross_check"]),
                    screen=SubstituteCheck(acfg, prcfg),
                    table=SubstituteTable(load_table(acfg)), template=template,
                    answer_template=answer_template, variant=variant,
                    a3_variant=a3_variant, a2_template=a2_template,
                    versions={"a1": a1_version, "a2": a2_version, "a3": a3_version},
                    resolve=catalog_resolver(config.pipeline_config()))
    return models

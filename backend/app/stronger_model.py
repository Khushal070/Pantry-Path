"""The stronger model: an optional hosted model that redoes A2 when the shopper says the
ingredients the local model worked out are wrong.

A2 (the ingredients of a dish the catalog doesn't have) runs on the champion, a small local
model (Qwen3-4B). It knows popular dishes but can miss regional ones: for dal dhokli it
picked the wrong dal and left out the peanuts. The result card then offers "Not right? Ask
a stronger model", which searches again with the data pipeline's own LLM client: the
provider, model and key in the repo-root .env (LLM_PROVIDER, LLM_MODEL, LLM_API_KEY).

Only the dish's name and the A2 prompt go to that provider, never store data. Verdicts
still come from diet_rules: a stronger model only proposes ingredients, as A2 always does.
Without LLM_API_KEY (or in demo mode) the option is off and the app works as before.
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.engines.base import EngineError

log = logging.getLogger(__name__)

PROVIDER_LABELS = {"gemini": "Gemini", "anthropic": "Claude", "openai": "OpenAI"}


@dataclass(frozen=True)
class StrongerModel:
    """Whether a stronger model is set up, and the name the button shows."""

    available: bool
    label: str | None = None      # "Gemini": what the shopper sees
    provider: str | None = None
    model: str | None = None      # e.g. gemini-3.6-flash (server side only)
    reason: str | None = None     # why it's off, for the server log and /api/config

    def view(self) -> dict[str, Any]:
        return {"available": self.available, "label": self.label}


OFF = StrongerModel(available=False, reason="not offered by this engine")


def detect(cfg: Any) -> StrongerModel:
    """The stronger model the .env sets up, resolved the way the pipeline's get_client does
    (env LLM_* wins over config/pipeline.yaml), without making a request."""
    from pantrypath_pipeline.config import secret
    from pantrypath_pipeline.errors import LLMError
    from pantrypath_pipeline.llm import detect_provider

    key = secret("LLM_API_KEY")
    if not key:
        return StrongerModel(False, reason="LLM_API_KEY is empty")
    settings = cfg["llm"]
    choice = (secret("LLM_PROVIDER") or str(settings.get("provider") or "auto")).strip().lower()
    if choice == "replay":
        return StrongerModel(False, reason="llm.provider is replay")
    try:
        provider = detect_provider(key, None if choice == "auto" else choice)
    except LLMError as exc:
        return StrongerModel(False, reason=str(exc))
    model = secret("LLM_MODEL") or (settings.get("models") or {}).get(provider)
    if not model:
        return StrongerModel(False, provider=provider,
                             reason=f"no model set for {provider}: set LLM_MODEL in .env")
    return StrongerModel(True, label=PROVIDER_LABELS.get(provider, provider.capitalize()),
                         provider=provider, model=model)


def error_for(exc: Exception, label: str) -> EngineError:
    """A failed stronger-model call as something the shopper can act on. The provider's
    message names the HTTP status; the reply text never reaches the shopper."""
    text = str(exc)
    if "HTTP 429" in text:
        return EngineError(f"{label}'s request limit is used up for now (the free tier allows "
                           "only a few requests a day). Try again later, or keep the first "
                           "answer above.", kind="stronger_quota")
    if "HTTP 503" in text or "UNAVAILABLE" in text or "high demand" in text:
        return EngineError(f"{label} is busy right now. Try again in a minute.",
                           kind="stronger_busy")
    if "HTTP 404" in text:
        log.warning("Stronger model not found: %s", text[:300])
        return EngineError(f"The {label} model set on this server isn't available any more: "
                           "set a current one in LLM_MODEL in .env.", kind="stronger_failed")
    log.warning("Stronger model failed: %s", text[:300])
    return EngineError(f"{label} couldn't work out the ingredients this time. Try again, or "
                       "keep the first answer above.", kind="stronger_failed")

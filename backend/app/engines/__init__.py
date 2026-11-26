"""The engines behind the chat: live (the real models and store search) or demo (offline)."""

from app.engines.base import Engine, EngineError, Issue, ParseOutcome, SearchOutcome
from app.settings import Settings

__all__ = ["Engine", "EngineError", "Issue", "ParseOutcome", "SearchOutcome", "make_engine"]


def make_engine(settings: Settings) -> Engine:
    if settings.mode == "live":
        from app.engines.live import LiveEngine

        return LiveEngine()
    from app.engines.demo import DemoEngine

    return DemoEngine(delay_s=settings.demo_delay_s)

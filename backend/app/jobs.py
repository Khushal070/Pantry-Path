"""Searches in the background, and their results in memory only.

A search takes seconds to minutes (store APIs; A2 for an unseen dish; A3, which is slow on
CPU), so it runs in a thread and the app polls it. Its result holds Kroger data, which
Kroger's terms forbid storing (docs/data-pipeline-handoff.md, section 10). So it lives
only here, in this process's memory, until it expires (PANTRYPATH_RESULT_TTL_MIN, and
never past result.json's own expires_at). It is never written to disk and never logged.
A restart forgets every result; the chat keeps the confirmed request, so the shopper can
search again with one click.
"""

import logging
import secrets
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.engines.base import EngineError, Progress, SearchOutcome

log = logging.getLogger(__name__)


@dataclass
class JobState:
    id: str
    created_at: datetime
    owner: str | None = None   # the user whose search it is: only they may read it
    status: str = "running"    # running | done | failed
    stage: str = "queued"      # queued | searching | answering | done | failed
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    outcome: SearchOutcome | None = None
    error: str | None = None
    error_kind: str | None = None

    def view(self) -> dict[str, Any]:
        o = self.outcome
        return {"id": self.id, "status": self.status, "stage": self.stage,
                "error": self.error, "error_kind": self.error_kind,
                "created_at": self.created_at, "finished_at": self.finished_at,
                "expires_at": self.expires_at,
                "result": o.result if o else None, "answer": o.answer if o else None,
                "answer_status": o.answer_status if o else None,
                "warnings": o.warnings if o else []}


def _result_expiry(outcome: SearchOutcome) -> datetime | None:
    try:
        return datetime.fromisoformat(outcome.result["meta"]["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None


class Jobs:
    def __init__(self, workers: int = 2, ttl_minutes: int = 60):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search")
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()
        self.ttl = timedelta(minutes=ttl_minutes)

    def create(self, owner: str | None = None) -> JobState:
        """Register a job (status running, stage queued); submit() starts it."""
        job = JobState(id="j_" + secrets.token_hex(8), created_at=datetime.now(UTC),
                       owner=owner)
        with self._lock:
            self._purge()
            self._jobs[job.id] = job
        return job

    def submit(self, job: JobState, run: Callable[[Progress], SearchOutcome],
               on_finish: Callable[[JobState], None] | None = None) -> None:
        self._pool.submit(self._run, job, run, on_finish)

    def _run(self, job: JobState, run: Callable[[Progress], SearchOutcome],
             on_finish: Callable[[JobState], None] | None) -> None:
        def progress(stage: str) -> None:
            job.stage = stage

        try:
            job.outcome = run(progress)
            job.stage, job.status = "done", "done"
        except EngineError as exc:
            job.error, job.error_kind = str(exc), exc.kind
            job.stage, job.status = "failed", "failed"
        except Exception as exc:
            # Type and place only: an exception message may quote product data
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            log.error("Search %s failed: %s at %s:%s", job.id, type(exc).__name__,
                      frame.filename, frame.lineno)
            job.error = "Something went wrong during the search. Please try again."
            job.error_kind = "error"
            job.stage, job.status = "failed", "failed"
        job.finished_at = datetime.now(UTC)
        expiry = job.finished_at + self.ttl
        if job.outcome and (result_expiry := _result_expiry(job.outcome)):
            expiry = min(expiry, result_expiry)
        job.expires_at = expiry
        if on_finish is not None:
            try:
                on_finish(job)
            except Exception:
                log.exception("Updating the chat after search %s failed", job.id)

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            self._purge()
            return self._jobs.get(job_id)

    def _purge(self) -> None:
        now = datetime.now(UTC)
        for job_id in [j.id for j in self._jobs.values() if j.expires_at and j.expires_at <= now]:
            del self._jobs[job_id]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

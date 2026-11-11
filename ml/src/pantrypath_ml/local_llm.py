"""LocalLLMClient: a self-hosted model behind an OpenAI-compatible server, as an LLMClient.

Servers and candidates come from configs/candidates.yaml; MODEL_SERVER_URL and MODEL_NAME
override the base URL and model id for one run, and MODEL_SERVER_TOKEN adds a bearer token
(a private Cloud Run service). Replies are held to the request's JSON schema through
`response_format` (/v1/chat/completions) or, for Ollama, through its `format` field
(native /api/chat, because Ollama's /v1 ignores `format`).

The data pipeline's cache key ignores which model answered, so cached_client gives each
setting its own cache directory: a new model can never replay another model's answers.
RecordingClient keeps per-call latency, tokens and cache hits for the evaluations, since
the pipeline functions they call return only their result.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from pantrypath_ml import config
from pantrypath_pipeline.config import Config, secret
from pantrypath_pipeline.errors import LLMError
from pantrypath_pipeline.llm import (
    CachedClient,
    LLMClient,
    LLMReply,
    LLMReplyError,
    ResponseCache,
    Usage,
    parse_json_object,
)

log = logging.getLogger(__name__)

JSON_MODES = ("response_format", "ollama_format")
NO_THINK = "\n/no_think"  # Qwen3's switch to answer without thinking


@dataclass
class LocalLLMReply(LLMReply):
    """An LLMReply plus the call's wall-clock time in seconds, retries included."""

    latency_s: float = 0.0


@dataclass(frozen=True)
class Candidate:
    name: str
    server: str  # a key under `servers` in candidates.yaml
    model: str
    quantization: str
    params_b: float = 0.0


def load_candidates(path: str | Path | None = None) -> Config:
    """configs/candidates.yaml, with relative paths resolved against the ml/ folder."""
    return config.load("candidates", path)


def get_candidate(cfg: Config, name: str, *, server: str | None = None,
                  quantization: str | None = None) -> Candidate:
    """A candidate on one server at one quantization; MODEL_NAME replaces its model id."""
    candidates = cfg["candidates"]
    if name not in candidates:
        raise LLMError(f"Unknown candidate {name!r}; choose one of {sorted(candidates)} "
                       "in configs/candidates.yaml")
    entry = candidates[name]
    server = server or cfg["default_server"]
    quantization = quantization or entry["default_quantization"]
    ids = (entry["quantizations"] or {}).get(quantization)
    if not ids or server not in ids:
        raise LLMError(f"Candidate {name} has no model id for quantization {quantization!r} "
                       f"on server {server!r} in configs/candidates.yaml")
    model = secret("MODEL_NAME") or ids[server]
    if model != ids[server]:
        log.info("Candidate %s: MODEL_NAME overrides model %s with %s", name, ids[server], model)
    return Candidate(name, server, model, quantization, float(entry.get("params_b") or 0))


def client_settings(cfg: Config, overrides: dict[str, Any] | None = None, *,
                    task: str | None = None) -> dict[str, Any]:
    """configs/candidates.yaml `client`, with per-run overrides (temperature, think).

    With a task ('a1', 'a2') and thinking off, max_output_tokens is that task's cap from
    max_output_tokens_no_think; with thinking on it stays max_output_tokens.
    """
    settings = {**cfg["client"], **{k: v for k, v in (overrides or {}).items() if v is not None}}
    settings["temperature"] = float(settings["temperature"])
    settings["think"] = bool(settings["think"])
    caps = settings.get("max_output_tokens_no_think") or {}
    if task and not settings["think"] and task in caps:
        settings["max_output_tokens"] = int(caps[task])
    return settings


# ---------------------------------------------------------------- HTTP


class _TransientError(Exception):
    """Rate limit, server error or network problem: worth another try."""


def _snippet(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _post_json(url: str, *, body: dict, model: str, settings: dict[str, Any]) -> dict:
    """POST with retries on 429, 5xx and network errors; other errors fail at once.

    A read timeout (the server took the request but no reply came in time) is tried at
    most read_timeout_attempts times, within max_attempts.
    """
    attempts = int(settings.get("max_attempts", 4))
    read_timeout_attempts = int(settings.get("read_timeout_attempts", attempts))
    read_timeouts = 0
    token = secret("MODEL_SERVER_TOKEN")
    extra = {"headers": {"Authorization": f"Bearer {token}"}} if token else {}

    @retry(
        retry=retry_if_exception_type(_TransientError),
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=1, max=float(settings.get("retry_wait_max_s", 60))),
        reraise=True,
    )
    def attempt() -> dict:
        nonlocal read_timeouts
        timeout = float(settings.get("timeout_s", 300))
        try:
            resp = requests.post(url, json=body, timeout=timeout, **extra)
        except requests.ReadTimeout as exc:
            read_timeouts += 1
            if read_timeouts >= read_timeout_attempts:
                raise LLMError(f"Model server at {url} sent no reply within {timeout:g}s, "
                               f"{read_timeouts} times; the reply may be running away "
                               "(check client.max_output_tokens)") from exc
            raise _TransientError(f"network error: {exc}") from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise _TransientError(f"network error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _TransientError(f"HTTP {resp.status_code}: {_snippet(resp.text)}")
        if resp.status_code in (401, 403):
            raise LLMError(f"The model server at {url} refused the request (HTTP "
                           f"{resp.status_code}); check MODEL_SERVER_TOKEN")
        if resp.status_code == 404:
            raise LLMError(f"The model server at {url} has no model {model!r} (HTTP 404); "
                           f"pull it (ollama pull {model}) or check MODEL_NAME")
        if resp.status_code >= 400:
            raise LLMError(f"Model server request failed (HTTP {resp.status_code}): "
                           f"{_snippet(resp.text)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise _TransientError("the model server answered with something that isn't JSON") \
                from exc

    try:
        return attempt()
    except _TransientError as exc:
        raise LLMError(f"Model server at {url} still failing after {attempts} attempts "
                       f"(is it running? check MODEL_SERVER_URL): {exc}") from exc


# ---------------------------------------------------------------- client


class LocalLLMClient:
    """A self-hosted model through Ollama, vLLM or llama.cpp, JSON constrained by the schema."""

    def __init__(self, base_url: str, model: str, settings: dict[str, Any], *,
                 server: str, json_mode: str):
        if json_mode not in JSON_MODES:
            raise LLMError(f"json_mode {json_mode!r} is not supported; use one of {JSON_MODES}")
        self.provider = server
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.json_mode = json_mode
        self.settings = settings

    def _request(self, prompt: str, schema: dict, purpose: str) -> tuple[str, dict]:
        temperature = float(self.settings["temperature"])
        think = bool(self.settings.get("think", False))
        max_tokens = int(self.settings.get("max_output_tokens", 8192))
        if self.json_mode == "ollama_format":
            return f"{self.base_url}/api/chat", {
                "model": self.model, "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": schema, "think": think,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
        content = prompt if think else prompt + NO_THINK
        name = re.sub(r"[^A-Za-z0-9_-]", "_", purpose)[:64] or "reply"
        return f"{self.base_url}/v1/chat/completions", {
            "model": self.model, "messages": [{"role": "user", "content": content}],
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
        }

    def _read(self, payload: dict) -> tuple[str, Usage, str | None, str | None]:
        """(text, usage, finish reason, refusal) from either reply format."""
        if self.json_mode == "ollama_format":
            usage = Usage(input_tokens=int(payload.get("prompt_eval_count") or 0),
                          output_tokens=int(payload.get("eval_count") or 0))
            message = payload.get("message") or {}
            return message.get("content") or "", usage, payload.get("done_reason"), None
        u = payload.get("usage") or {}
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage = Usage(
            input_tokens=max(int(u.get("prompt_tokens") or 0) - cached, 0),
            output_tokens=int(u.get("completion_tokens") or 0),  # includes thinking tokens
            cache_read_tokens=cached,
        )
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return (message.get("content") or "", usage, choice.get("finish_reason"),
                message.get("refusal"))

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LocalLLMReply:
        url, body = self._request(prompt, schema, purpose)
        start = time.perf_counter()
        payload = _post_json(url, body=body, model=self.model, settings=self.settings)
        latency = round(time.perf_counter() - start, 3)
        text, usage, finish, refusal = self._read(payload)
        reply = LocalLLMReply(data={}, usage=usage, model=payload.get("model") or self.model,
                              provider=self.provider, raw_text=text, latency_s=latency)
        log.info("LLM call %s: model=%s input=%d output=%d latency=%.2fs",
                 purpose, reply.model, usage.input_tokens, usage.output_tokens, latency)
        if refusal:
            raise LLMReplyError(f"The model declined the request: {refusal}", reply)
        if finish == "length":
            raise LLMReplyError("The model reply was cut off; raise client.max_output_tokens "
                                "or the server's context length", reply)
        if finish not in (None, "stop"):
            raise LLMReplyError(f"The model stopped early (finish reason {finish})", reply)
        try:
            reply.data = parse_json_object(text)
        except ValueError as exc:
            raise LLMReplyError(f"The LLM reply is not a JSON object: {exc}", reply) from exc
        return reply


def make_client(cfg: Config, candidate: Candidate,
                settings: dict[str, Any] | None = None) -> LocalLLMClient:
    """The client for a candidate; MODEL_SERVER_URL, when set, replaces the server's URL."""
    servers = cfg["servers"]
    if candidate.server not in servers:
        raise LLMError(f"Candidate {candidate.name} names server {candidate.server!r}, which "
                       f"is not under servers in configs/candidates.yaml")
    server = servers[candidate.server]
    base_url = secret("MODEL_SERVER_URL") or server["base_url"]
    log.info("LLM: %s at %s, model %s (%s)", candidate.server, base_url, candidate.model,
             candidate.quantization)
    return LocalLLMClient(base_url, candidate.model, settings or client_settings(cfg),
                          server=candidate.server, json_mode=server["json_mode"])


# ---------------------------------------------------------------- per-setting cache


def cache_dir_for(cfg: Config, candidate: Candidate, prompt_version: str,
                  settings: dict[str, Any] | None = None) -> Path:
    """A cache folder unique to model, quantization, prompt version, temperature and think.

    The readable part can collide after cleanup ("a:b" and "a_b"), so a hash of the exact
    values is appended. The hash also covers the server's JSON mode, which decides whether
    the think switch takes effect.
    """
    settings = settings or client_settings(cfg)
    temperature, think = float(settings["temperature"]), bool(settings["think"])
    json_mode = (cfg["servers"].get(candidate.server) or {}).get("json_mode")
    identity = {"model": candidate.model, "quantization": candidate.quantization,
                "prompt_version": prompt_version, "temperature": temperature, "think": think,
                "json_mode": json_mode}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    parts = (candidate.model, candidate.quantization, prompt_version, f"t{temperature:g}",
             "think" if think else "nothink")
    readable = "__".join(re.sub(r"[^A-Za-z0-9.-]+", "_", p) for p in parts)
    return cfg.path(cfg["client"]["cache_dir"]) / f"{readable}__{digest}"


def cached_client(cfg: Config, candidate: Candidate, prompt_version: str, *,
                  settings: dict[str, Any] | None = None,
                  run_id: str | None = None) -> CachedClient:
    """The candidate's client behind the pipeline's CachedClient, in its own cache folder.

    With run_id the cache starts empty in a subfolder for that run, so an evaluation
    measures real calls; replies stay on disk as a record of the run.
    """
    settings = settings or client_settings(cfg)
    folder = cache_dir_for(cfg, candidate, prompt_version, settings)
    if run_id:
        folder = folder / "runs" / run_id
    return CachedClient(make_client(cfg, candidate, settings), ResponseCache(folder))


# ---------------------------------------------------------------- per-call records


@dataclass
class RecordingClient:
    """Wraps a client and records each call's latency, tokens, cache hit and error."""

    inner: LLMClient
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def _record(self, start: float, reply: LLMReply | None, error: str | None) -> None:
        usage = reply.usage if reply else Usage()
        self.calls.append({
            "latency_s": round(time.perf_counter() - start, 3),
            "cached": bool(reply and reply.cached),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "error": error,
        })

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        start = time.perf_counter()
        try:
            reply = self.inner.complete_json(prompt, schema, purpose=purpose)
        except LLMReplyError as exc:
            self._record(start, exc.reply, str(exc))
            raise
        except LLMError as exc:
            self._record(start, None, str(exc))
            raise
        self._record(start, reply, None)
        return reply

    def take(self) -> dict[str, Any]:
        """Totals of the calls since the last take(), then start a new tally."""
        calls, self.calls = self.calls, []
        return {
            "calls": len(calls),
            "cached_calls": sum(c["cached"] for c in calls),
            "latency_s": round(sum(c["latency_s"] for c in calls), 3),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
        }

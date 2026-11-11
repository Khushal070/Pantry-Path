"""LocalLLMClient against a fake HTTP server: request shapes, think, replies, retries,
cache folders and the recording wrapper.

No test reaches a real model server: requests.post is replaced by FakeHTTP.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from pantrypath_ml import local_llm
from pantrypath_ml.local_llm import (
    LocalLLMClient,
    RecordingClient,
    cache_dir_for,
    cached_client,
    client_settings,
    get_candidate,
    load_candidates,
    make_client,
)
from pantrypath_pipeline.errors import LLMError
from pantrypath_pipeline.llm import LLMReplyError, Usage, summarize_usage

SCHEMA = {
    "type": "object",
    "properties": {"dish": {"type": "string"}},
    "required": ["dish"],
    "additionalProperties": False,
}


class FakeHTTP:
    """Stands in for requests.post: records requests, answers from a list.

    Each answer is (status, payload), or an exception instance to raise.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "json": json, "timeout": timeout, **kwargs})
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer
        return SimpleNamespace(status_code=status, json=lambda: payload, text=str(payload))


@pytest.fixture
def http(monkeypatch):
    def install(responses):
        fake = FakeHTTP(responses)
        monkeypatch.setattr(local_llm.requests, "post", fake.post)
        return fake
    return install


def openai_payload(content, finish="stop", model="qwen3:4b-q4_K_M"):
    return {"model": model,
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 250,
                      "prompt_tokens_details": {"cached_tokens": 100}}}


def ollama_payload(content, done_reason="stop"):
    return {"model": "qwen3:4b-q4_K_M", "message": {"role": "assistant", "content": content},
            "done": True, "done_reason": done_reason, "prompt_eval_count": 700,
            "eval_count": 120}


@pytest.fixture
def cfg(cfg):
    """Most tests exercise the OpenAI-compatible mode; the Ollama-native one has its own."""
    cfg.data["servers"]["ollama"]["json_mode"] = "response_format"
    return cfg


def qwen(cfg, **overrides):
    return make_client(cfg, get_candidate(cfg, "qwen3-4b"), client_settings(cfg, overrides))


# ---------------------------------------------------------------- config


def test_candidates_file_loads():
    cfg = load_candidates()
    candidate = get_candidate(cfg, "qwen3-4b")
    assert (candidate.server, candidate.model, candidate.quantization, candidate.params_b) == (
        "ollama", "qwen3:4b-q4_K_M", "4bit", 4.0)
    assert get_candidate(cfg, "qwen3-4b", quantization="8bit").model == "qwen3:4b-q8_0"
    assert get_candidate(cfg, "qwen3-4b", server="vllm").model == "Qwen/Qwen3-4B-AWQ"


def test_env_overrides_server_url_and_model(cfg, monkeypatch):
    monkeypatch.setenv("MODEL_SERVER_URL", "http://gpu-node:8000/")
    monkeypatch.setenv("MODEL_NAME", "qwen3:4b-fp16")
    client = qwen(cfg)
    assert client.base_url == "http://gpu-node:8000"
    assert client.model == "qwen3:4b-fp16"


def test_unknown_candidate_quantization_server_and_json_mode(cfg):
    with pytest.raises(LLMError, match="Unknown candidate"):
        get_candidate(cfg, "gpt-5")
    with pytest.raises(LLMError, match="no model id for quantization"):
        get_candidate(cfg, "qwen3-4b", quantization="2bit")
    with pytest.raises(LLMError, match="not under servers"):
        make_client(cfg, replace(get_candidate(cfg, "qwen3-4b"), server="llamacpp"))
    with pytest.raises(LLMError, match="json_mode"):
        LocalLLMClient("http://x", "m", cfg["client"], server="ollama", json_mode="grammar")


def test_overrides_skip_none_and_coerce_types(cfg):
    settings = client_settings(cfg, {"temperature": 1, "think": None})
    assert settings["temperature"] == 1.0 and settings["think"] is False


def test_output_cap_per_task_without_thinking(cfg):
    assert client_settings(cfg, task="a1")["max_output_tokens"] == 256
    assert client_settings(cfg, task="a2")["max_output_tokens"] == 1536
    assert client_settings(cfg, {"think": True}, task="a1")["max_output_tokens"] == 8192
    assert client_settings(cfg)["max_output_tokens"] == 8192


# ---------------------------------------------------------------- requests and replies


def test_valid_json_reply_and_request_shape(cfg, http):
    fake = http([(200, openai_payload('{"dish": "lasagna"}'))])
    reply = qwen(cfg).complete_json("prompt", SCHEMA, purpose="refine_dish:lasagna")
    assert reply.data == {"dish": "lasagna"}
    assert (reply.model, reply.provider) == ("qwen3:4b-q4_K_M", "ollama")
    sent = fake.requests[0]
    assert sent["url"] == "http://localhost:11434/v1/chat/completions"
    assert "headers" not in sent  # no API key, no token
    body = sent["json"]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "refine_dish_lasagna", "schema": SCHEMA, "strict": True}}
    assert body["temperature"] == 0 and body["max_tokens"] == cfg["client"]["max_output_tokens"]
    assert sent["timeout"] == cfg["client"]["timeout_s"]


def test_think_switch_in_response_format_mode(cfg, http):
    fake = http([(200, openai_payload('{"dish": "a"}')), (200, openai_payload('{"dish": "b"}'))])
    qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    qwen(cfg, think=True).complete_json("prompt", SCHEMA, purpose="t")
    assert fake.requests[0]["json"]["messages"][0]["content"] == "prompt\n/no_think"
    assert fake.requests[1]["json"]["messages"][0]["content"] == "prompt"


def test_ollama_format_mode_sends_think(cfg, http):
    cfg.data["servers"]["ollama"]["json_mode"] = "ollama_format"
    fake = http([(200, ollama_payload('{"dish": "pho"}'))])
    reply = qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert reply.data == {"dish": "pho"}
    assert reply.usage == Usage(input_tokens=700, output_tokens=120)
    sent = fake.requests[0]
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["json"]["format"] == SCHEMA and sent["json"]["stream"] is False
    assert sent["json"]["think"] is False
    assert sent["json"]["messages"][0]["content"] == "prompt"  # no /no_think here
    assert sent["json"]["options"] == {"temperature": 0.0,
                                       "num_predict": cfg["client"]["max_output_tokens"]}


def test_bearer_token_for_a_private_server(cfg, http, monkeypatch):
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "tok")
    fake = http([(200, openai_payload('{"dish": "a"}'))])
    qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert fake.requests[0]["headers"] == {"Authorization": "Bearer tok"}


def test_malformed_reply_raises_with_usage(cfg, http):
    http([(200, openai_payload("Sure! The dish is lasagna."))])
    with pytest.raises(LLMReplyError, match="not a JSON object") as info:
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert info.value.reply.usage.output_tokens == 250  # tokens were still spent


def test_json_that_is_not_an_object_is_malformed(cfg, http):
    http([(200, openai_payload('["lasagna"]'))])
    with pytest.raises(LLMReplyError, match="not a JSON object"):
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")


@pytest.mark.parametrize(("finish", "message"), [("length", "cut off"),
                                                 ("content_filter", "stopped early")])
def test_unusable_finish_reasons(cfg, http, finish, message):
    http([(200, openai_payload('{"dish": ', finish=finish))])
    with pytest.raises(LLMReplyError, match=message):
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")


# ---------------------------------------------------------------- retries and errors


def test_retry_on_5xx_and_network_errors(cfg, http):
    fake = http([(503, {"error": "loading model"}), requests.ConnectionError("refused"),
                 (200, openai_payload('{"dish": "ramen"}'))])
    assert qwen(cfg).complete_json("prompt", SCHEMA, purpose="t").data == {"dish": "ramen"}
    assert len(fake.requests) == 3


def test_a_read_timeout_is_retried_once(cfg, http):
    fake = http([requests.ReadTimeout("slow"), requests.ReadTimeout("slow"),
                 (200, openai_payload('{"dish": "ramen"}'))])
    with pytest.raises(LLMError, match="no reply within 300s, 2 times"):
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert len(fake.requests) == 2


def test_gives_up_after_max_attempts(cfg, http):
    attempts = cfg["client"]["max_attempts"]
    fake = http([(500, "down")] * attempts)
    with pytest.raises(LLMError, match=f"after {attempts} attempts"):
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert len(fake.requests) == attempts


@pytest.mark.parametrize(("status", "message"), [(404, "ollama pull qwen3:4b-q4_K_M"),
                                                 (403, "MODEL_SERVER_TOKEN")])
def test_client_errors_fail_at_once(cfg, http, status, message):
    fake = http([(status, {"error": "nope"})])
    with pytest.raises(LLMError, match=message):
        qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- usage and latency


def test_usage_and_latency_recorded(cfg, http, monkeypatch):
    http([(200, openai_payload('{"dish": "lasagna"}'))])
    clock = iter([100.0, 102.5])
    monkeypatch.setattr(local_llm.time, "perf_counter", lambda: next(clock))
    reply = qwen(cfg).complete_json("prompt", SCHEMA, purpose="t")
    assert reply.latency_s == 2.5
    assert reply.usage == Usage(input_tokens=800, output_tokens=250, cache_read_tokens=100)

    # The pipeline's usage report takes the reply as is; a local model has no price
    summary = summarize_usage([reply], prices={})
    assert summary["totals"]["calls_real"] == 1 and summary["totals"]["input_tokens"] == 800
    assert summary["models"]["qwen3:4b-q4_K_M"]["est_cost_usd"] is None


def test_recording_client_counts_calls_tokens_cache_hits_and_errors(cfg, http):
    http([(200, openai_payload('{"dish": "a"}')), (200, openai_payload("not json"))])
    client = RecordingClient(cached_client(cfg, get_candidate(cfg, "qwen3-4b"), "v1"))
    client.complete_json("p", SCHEMA, purpose="t")
    client.complete_json("p", SCHEMA, purpose="t")  # replayed from the cache
    with pytest.raises(LLMReplyError):
        client.complete_json("q", SCHEMA, purpose="t")
    totals = client.take()
    assert (totals["calls"], totals["cached_calls"]) == (3, 1)
    assert totals["input_tokens"] == 800 * 3 and totals["output_tokens"] == 250 * 3
    assert client.take()["calls"] == 0  # take() starts a new tally


# ---------------------------------------------------------------- per-setting cache


def test_two_models_never_share_cache_entries(cfg, http):
    qwen_4b = get_candidate(cfg, "qwen3-4b")
    other = replace(qwen_4b, name="other", model="other-model:4b")
    fake = http([(200, openai_payload('{"dish": "from qwen"}')),
                 (200, openai_payload('{"dish": "from other"}', model="other-model:4b"))])

    first = cached_client(cfg, qwen_4b, "refine_dish/1").complete_json("p", SCHEMA, purpose="t")
    second = cached_client(cfg, other, "refine_dish/1").complete_json("p", SCHEMA, purpose="t")
    assert len(fake.requests) == 2  # the second model was asked, not replayed
    assert first.data == {"dish": "from qwen"} and second.data == {"dish": "from other"}

    # Each model replays only its own answer
    again = cached_client(cfg, qwen_4b, "refine_dish/1").complete_json("p", SCHEMA, purpose="t")
    assert again.cached and again.data == {"dish": "from qwen"}
    assert len(fake.requests) == 2
    dirs = {cache_dir_for(cfg, c, "refine_dish/1") for c in (qwen_4b, other)}
    assert len(dirs) == 2
    for d in dirs:
        assert len(list(d.glob("*.json"))) == 1
        assert d.is_relative_to(cfg.path(cfg["client"]["cache_dir"]))


def test_cache_folder_depends_on_every_setting(cfg):
    base = get_candidate(cfg, "qwen3-4b")
    folder = cache_dir_for(cfg, base, "refine_dish/1")
    assert folder.name.startswith("qwen3_4b-q4_K_M__4bit__refine_dish_1__t0__nothink__")
    assert cache_dir_for(cfg, replace(base, quantization="8bit"), "refine_dish/1") != folder
    assert cache_dir_for(cfg, base, "refine_dish/2") != folder
    assert cache_dir_for(cfg, base, "refine_dish/1", client_settings(cfg, {"think": True})) \
        != folder
    assert cache_dir_for(cfg, base, "refine_dish/1",
                         client_settings(cfg, {"temperature": 0.5})) != folder
    cfg.data["servers"]["ollama"]["json_mode"] = "ollama_format"
    assert cache_dir_for(cfg, base, "refine_dish/1") != folder


def test_lookalike_model_ids_get_different_folders(cfg):
    base = get_candidate(cfg, "qwen3-4b")
    assert cache_dir_for(cfg, replace(base, model="a:b"), "v") != cache_dir_for(
        cfg, replace(base, model="a_b"), "v")


def test_a_run_gets_a_fresh_cache_folder(cfg, http):
    candidate = get_candidate(cfg, "qwen3-4b")
    fake = http([(200, openai_payload('{"dish": "a"}')), (200, openai_payload('{"dish": "b"}'))])
    cached_client(cfg, candidate, "v").complete_json("p", SCHEMA, purpose="t")
    run = cached_client(cfg, candidate, "v", run_id="run-1").complete_json("p", SCHEMA,
                                                                           purpose="t")
    assert not run.cached and len(fake.requests) == 2
    assert (cache_dir_for(cfg, candidate, "v") / "runs" / "run-1").is_dir()


def test_cached_unusable_reply_is_replayed_as_an_error(cfg, http):
    candidate = get_candidate(cfg, "qwen3-4b")
    fake = http([(200, openai_payload("not json"))])
    client = cached_client(cfg, candidate, "refine_dish/1")
    for _ in range(2):
        with pytest.raises(LLMReplyError, match="not a JSON object"):
            client.complete_json("p", SCHEMA, purpose="t")
    assert len(fake.requests) == 1
    record = json.loads(next(cache_dir_for(cfg, candidate, "refine_dish/1").glob("*.json"))
                        .read_text())
    assert record["model"] == "qwen3:4b-q4_K_M" and record["error"]

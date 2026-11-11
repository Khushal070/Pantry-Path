"""MLflow helper (temp local store) and Artifact Registry commands (fake runner, no GCP)."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import mlflow
import pytest

from pantrypath_ml import registry
from pantrypath_ml.local_llm import get_candidate
from pantrypath_pipeline.errors import PipelineError

AR = {"GCP_PROJECT_ID": "proj", "GCP_REGION": "us-east1", "AR_DOCKER_REPO": "docker",
      "AR_GENERIC_REPO": "generic"}


def test_tracking_uri_defaults_to_local_store_and_env_wins(monkeypatch):
    assert registry.tracking_uri() == f"sqlite:///{registry.LOCAL_STORE / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow.example:5000")
    assert registry.tracking_uri() == "http://mlflow.example:5000"


def test_run_has_standard_tags_and_stays_in_the_local_store(cfg):
    candidate = get_candidate(cfg, "qwen3-4b")
    with registry.start_run(task="a2", candidate=candidate, prompt_version="refine_dish/1",
                            split="dev") as run:
        mlflow.log_metric("recall", 0.8)
        mlflow.log_dict({"ok": True}, "summary.json")

    saved = mlflow.get_run(run.info.run_id)
    assert {k: v for k, v in saved.data.tags.items() if not k.startswith("mlflow.")} == {
        "task": "a2", "candidate": "qwen3-4b", "model": "qwen3:4b-q4_K_M",
        "quantization": "4bit", "prompt_version": "refine_dish/1", "git_commit": "abc123",
        "eval_split": "dev",
    }
    assert saved.data.metrics == {"recall": 0.8}
    assert mlflow.get_experiment(saved.info.experiment_id).name == "pantrypath-a2"
    assert (registry.LOCAL_STORE / "mlflow.db").is_file()
    assert list((registry.LOCAL_STORE / "artifacts").rglob("summary.json"))


class FakeRunner:
    def __init__(self, stdout=""):
        self.commands = []
        self.stdout = stdout

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd[:4] == ["gcloud", "artifacts", "generic", "download"]:
            dest = next(c.split("=", 1)[1] for c in cmd if c.startswith("--destination="))
            (Path(dest) / "champion.json").write_text('{"champion": 1}')
        return SimpleNamespace(stdout=self.stdout, returncode=0)


@pytest.fixture
def champion(tmp_path):
    path = tmp_path / "champion.json"
    path.write_text(json.dumps({"champion": {"models": {"ollama": "qwen3:4b-q4_K_M"}}}))
    return path


def test_artifact_registry_needs_every_setting(champion):
    with pytest.raises(PipelineError, match="GCP_PROJECT_ID, GCP_REGION"):
        registry.push_champion(champion, runner=FakeRunner())


def test_push_image_builds_with_the_champion_model(champion, monkeypatch):
    for k, v in AR.items():
        monkeypatch.setenv(k, v)
    runner = FakeRunner()
    uri = registry.push_image("abc", champion=champion, runner=runner)
    assert uri == "us-east1-docker.pkg.dev/proj/docker/pantrypath-model-server:abc"
    build, push = runner.commands
    assert build[:4] == ["docker", "build", "--build-arg", "MODEL=qwen3:4b-q4_K_M"]
    assert push == ["docker", "push", uri]


def test_no_champion_means_no_image(tmp_path, monkeypatch):
    for k, v in AR.items():
        monkeypatch.setenv(k, v)
    empty = tmp_path / "c.json"
    empty.write_text('{"champion": null}')
    with pytest.raises(PipelineError, match="no champion"):
        registry.push_image("t", champion=empty, runner=FakeRunner())


def test_champion_upload_and_newest_download(champion, tmp_path, monkeypatch):
    for k, v in AR.items():
        monkeypatch.setenv(k, v)
    runner = FakeRunner(stdout=json.dumps([{"name": "x/versions/v2"}, {"name": "x/versions/v1"}]))
    registry.push_champion(champion, version="v3", runner=runner)
    assert "--version=v3" in runner.commands[0] and "--repository=generic" in runner.commands[0]
    dest = tmp_path / "pulled.json"
    assert registry.pull_champion(dest, runner=runner) == "v2"
    assert json.loads(dest.read_text()) == {"champion": 1}


def test_a_failed_command_surfaces(champion, monkeypatch):
    for k, v in AR.items():
        monkeypatch.setenv(k, v)

    def failing(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="denied")
    with pytest.raises(subprocess.CalledProcessError):
        registry.push_champion(champion, runner=failing)

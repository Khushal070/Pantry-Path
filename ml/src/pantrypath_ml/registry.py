"""MLflow tracking, and the GCP Artifact Registry push of the champion.

MLflow: every evaluation run carries the same standard tags. MLFLOW_TRACKING_URI picks the
tracking server. Without it, runs go to a local store in ml/mlruns/: a SQLite file plus an
artifacts folder, because MLflow 3 refuses a plain folder store unless
MLFLOW_ALLOW_FILE_STORE is set.

Artifact Registry (all through the docker and gcloud CLIs, authenticated beforehand):
- the model-server image (ml/serving/), built with the champion's Ollama model id, goes to
  a Docker repository;
- the champion record (registry/champion.json: model id, quantization, prompt versions,
  settings, metrics) goes to a generic repository, one version per promotion.
Project, region and repositories come from GCP_PROJECT_ID, GCP_REGION, AR_DOCKER_REPO and
AR_GENERIC_REPO; nothing here guesses them.

    python -m pantrypath_ml.registry push-image --tag TAG
    python -m pantrypath_ml.registry push-champion
    python -m pantrypath_ml.registry pull-champion [--version V]
"""

import argparse
import json
import logging
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow

from pantrypath_ml.config import ML_HOME
from pantrypath_ml.local_llm import Candidate
from pantrypath_pipeline.config import secret
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

LOCAL_STORE = ML_HOME / "mlruns"


def _local_uri() -> str:
    return f"sqlite:///{LOCAL_STORE / 'mlflow.db'}"


def tracking_uri() -> str:
    return secret("MLFLOW_TRACKING_URI") or _local_uri()


def git_commit() -> str:
    """The checked-out commit, or 'unknown' outside a git checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ML_HOME, capture_output=True,
                             text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def _experiment_id(name: str, *, local: bool) -> str:
    client = mlflow.MlflowClient()
    experiment = client.get_experiment_by_name(name)
    if experiment is not None:
        return experiment.experiment_id
    # A local store keeps its artifacts next to the database, not in the working directory
    location = (LOCAL_STORE / "artifacts").as_uri() if local else None
    return client.create_experiment(name, artifact_location=location)


@contextmanager
def start_run(*, task: str, candidate: Candidate, prompt_version: str, split: str,
              run_name: str | None = None) -> Iterator[mlflow.ActiveRun]:
    """An MLflow run in experiment 'pantrypath-<task>' with the standard tags."""
    uri = tracking_uri()
    # Compared by value: mlflow.set_tracking_uri also sets MLFLOW_TRACKING_URI
    local = uri == _local_uri()
    if local:
        LOCAL_STORE.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(uri)
    tags = {
        "task": task,
        "candidate": candidate.name,
        "model": candidate.model,
        "quantization": candidate.quantization,
        "prompt_version": prompt_version,
        "git_commit": git_commit(),
        "eval_split": split,
    }
    with mlflow.start_run(experiment_id=_experiment_id(f"pantrypath-{task}", local=local),
                          run_name=run_name, tags=tags) as run:
        log.info("MLflow run %s in %s (%s)", run.info.run_id, f"pantrypath-{task}", uri)
        yield run


# ---------------------------------------------------------------- Artifact Registry

AR_ENV = ("GCP_PROJECT_ID", "GCP_REGION", "AR_DOCKER_REPO", "AR_GENERIC_REPO")
IMAGE_NAME = "pantrypath-model-server"
PACKAGE = "champion"
Runner = Callable[..., subprocess.CompletedProcess]


def ar_settings() -> dict[str, str]:
    """The Artifact Registry settings from the environment; all of them are required."""
    values = {name: secret(name) for name in AR_ENV}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PipelineError(f"Set {', '.join(missing)} (GitHub repository variables in CI, "
                            "the environment locally) to use Artifact Registry")
    return values  # type: ignore[return-value]


def image_uri(settings: dict[str, str], tag: str) -> str:
    return (f"{settings['GCP_REGION']}-docker.pkg.dev/{settings['GCP_PROJECT_ID']}/"
            f"{settings['AR_DOCKER_REPO']}/{IMAGE_NAME}:{tag}")


def _run(runner: Runner, cmd: list[str]) -> subprocess.CompletedProcess:
    log.info("Running: %s", " ".join(cmd))
    return runner(cmd, check=True, capture_output=True, text=True)


def champion_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text()).get("champion")
    if not record:
        raise PipelineError(f"{path} has no champion yet; promote one first")
    return record


def push_image(tag: str, *, champion: Path, runner: Runner = subprocess.run) -> str:
    """Build the model-server image for the champion's Ollama model and push it."""
    settings = ar_settings()
    model = champion_record(champion)["models"]["ollama"]
    uri = image_uri(settings, tag)
    _run(runner, ["docker", "build", "--build-arg", f"MODEL={model}", "-t", uri,
                  str(ML_HOME / "serving")])
    _run(runner, ["docker", "push", uri])
    return uri


def push_champion(champion: Path, *, version: str | None = None,
                  runner: Runner = subprocess.run) -> str:
    """Upload the champion record as a new version of the generic package."""
    settings = ar_settings()
    version = version or f"{datetime.now(UTC):%Y%m%dt%H%M%S}"
    _run(runner, ["gcloud", "artifacts", "generic", "upload",
                  f"--project={settings['GCP_PROJECT_ID']}", f"--location={settings['GCP_REGION']}",
                  f"--repository={settings['AR_GENERIC_REPO']}", f"--package={PACKAGE}",
                  f"--version={version}", f"--source={champion}"])
    return version


def versions(*, runner: Runner = subprocess.run) -> list[str]:
    """Champion versions in the generic repository, newest first."""
    settings = ar_settings()
    out = _run(runner, ["gcloud", "artifacts", "versions", "list",
                        f"--project={settings['GCP_PROJECT_ID']}",
                        f"--location={settings['GCP_REGION']}",
                        f"--repository={settings['AR_GENERIC_REPO']}", f"--package={PACKAGE}",
                        "--sort-by=~createTime", "--format=json"])
    return [v["name"].rsplit("/", 1)[-1] for v in json.loads(out.stdout or "[]")]


def pull_champion(dest: Path, *, version: str | None = None,
                  runner: Runner = subprocess.run) -> str | None:
    """Download a champion version (default: the newest) to dest; None when there is none."""
    settings = ar_settings()
    if version is None:
        found = versions(runner=runner)
        if not found:
            return None
        version = found[0]
    folder = dest.parent / f".download-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    _run(runner, ["gcloud", "artifacts", "generic", "download",
                  f"--project={settings['GCP_PROJECT_ID']}", f"--location={settings['GCP_REGION']}",
                  f"--repository={settings['AR_GENERIC_REPO']}", f"--package={PACKAGE}",
                  f"--version={version}", f"--destination={folder}"])
    downloaded = next(folder.glob("*.json"))
    downloaded.replace(dest)
    folder.rmdir()
    return version


def main(argv: list[str] | None = None) -> int:
    from pantrypath_ml.select import CHAMPION

    parser = argparse.ArgumentParser(prog="pantrypath_ml.registry",
                                     description="Artifact Registry push and pull")
    parser.add_argument("command", choices=["push-image", "push-champion", "pull-champion"])
    parser.add_argument("--tag", help="push-image: the image tag (e.g. the git commit)")
    parser.add_argument("--version", help="pull-champion: a version (default: newest)")
    parser.add_argument("--champion", type=Path, default=CHAMPION)
    args = parser.parse_args(argv)
    setup_logging()
    try:
        if args.command == "push-image":
            if not args.tag:
                parser.error("push-image needs --tag")
            print(push_image(args.tag, champion=args.champion))
        elif args.command == "push-champion":
            print(push_champion(args.champion))
        else:
            print(pull_champion(args.champion, version=args.version) or "none")
    except (PipelineError, subprocess.CalledProcessError) as exc:
        log.error("registry %s failed: %s", args.command, getattr(exc, "stderr", None) or exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

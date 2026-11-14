"""serving/rollback.sh with fake gcloud and python executables (no GCP)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "serving" / "rollback.sh"

# Answers like gcloud for the calls rollback.sh makes; every call is logged
FAKE_GCLOUD = """#!/usr/bin/env bash
echo "gcloud $*" >> "$CALLS"
case "$*" in
  "run services describe"*) echo rev-00002 ;;
  "run revisions list"*) printf 'rev-00002\\nrev-00001\\n' ;;
  "artifacts versions list"*) printf 'p/champion/versions/v2\\np/champion/versions/v1\\n' ;;
esac
"""
FAKE_PYTHON = """#!/usr/bin/env bash
echo "python $*" >> "$CALLS"
"""


@pytest.fixture
def run(tmp_path):
    """Run a copy of rollback.sh in a temporary ml/ folder; returns (result, calls)."""
    ml = tmp_path / "ml"
    (ml / "serving").mkdir(parents=True)
    (ml / "registry").mkdir()
    shutil.copy(SCRIPT, ml / "serving" / "rollback.sh")
    (ml / "registry" / "champion.json").write_text('{"champion": "current"}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("python", FAKE_PYTHON)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls.log"

    def _run(**env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        full_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CALLS": str(calls),
                    "GCP_PROJECT_ID": "proj", "GCP_REGION": "us-east1",
                    "CLOUD_RUN_SERVICE": "svc", "AR_GENERIC_REPO": "generic", **env}
        result = subprocess.run(["bash", str(ml / "serving" / "rollback.sh")], env=full_env,
                                capture_output=True, text=True)
        log = calls.read_text().splitlines() if calls.exists() else []
        return result, log

    _run.champion = ml / "registry" / "champion.json"
    return _run


def test_traffic_only_when_restore_champion_is_off(run):
    result, calls = run(RESTORE_CHAMPION="0")
    assert result.returncode == 0, result.stderr
    assert any("update-traffic svc" in c and "--to-revisions rev-00001=100" in c for c in calls)
    assert not any(c.startswith("gcloud artifacts") or c.startswith("python") for c in calls)
    assert run.champion.read_text() == '{"champion": "current"}'
    assert "champion record left as is" in result.stdout


def test_restores_the_previous_champion_by_default(run):
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert any("update-traffic svc" in c for c in calls)
    assert any(c.startswith("gcloud artifacts versions list") for c in calls)
    assert "python -m pantrypath_ml.registry pull-champion --version v1" in calls
    assert calls[-1] == "python -m pantrypath_ml.registry push-champion"

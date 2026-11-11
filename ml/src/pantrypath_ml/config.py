"""Settings: YAML files in ml/configs/, secrets from the repo-root .env.

Relative paths in these files resolve against the ml/ folder. The data pipeline's own
config (data-pipeline/config/pipeline.yaml) is loaded with its own loader, so its paths
keep resolving against data-pipeline/.
"""

from pathlib import Path

import yaml

from pantrypath_pipeline.config import Config, load_config, load_env

ML_HOME = Path(__file__).resolve().parents[2]
REPORTS = ML_HOME / "reports"


def load(name: str, path: str | Path | None = None) -> Config:
    """configs/<name>.yaml (or `path`) as a Config rooted at ml/."""
    load_env(ML_HOME)
    with open(path or ML_HOME / "configs" / f"{name}.yaml") as f:
        return Config(yaml.safe_load(f), ML_HOME)


def pipeline_config() -> Config:
    """The data pipeline's pipeline.yaml (catalog paths, catalog_eval, llm prompt)."""
    return load_config()

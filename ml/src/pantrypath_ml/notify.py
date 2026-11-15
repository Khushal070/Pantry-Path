"""Notifications for the model pipeline: the data pipeline's email alerts, plus Slack.

The email side is alerts.send_alert with configs/alerts.yaml (always logged; emailed when
ALERT_* are set). Slack gets the same text when SLACK_WEBHOOK_URL is set. Neither ever
raises: a broken notification must not hide the job's real result.

    python -m pantrypath_ml.notify --event failure|completed|gate_failed \
        --subject "..." [--body-file final.md]
"""

import argparse
import logging
from pathlib import Path

import requests

from pantrypath_ml import config
from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import secret
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

LEVELS = {"failure": "error", "gate_failed": "warning", "completed": "info"}


def post_slack(text: str) -> bool:
    url = secret("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Slack notification failed: %s", type(exc).__name__)  # never the URL
        return False
    return True


def notify(event: str, subject: str, body: str) -> dict[str, bool]:
    level = LEVELS[event]
    emailed = send_alert(subject, body, level=level, cfg=config.load("alerts"))
    slacked = post_slack(f"*{level.upper()}* {subject}\n{body}")
    return {"emailed": emailed, "slack": slacked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.notify",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--event", choices=sorted(LEVELS), required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", type=Path)
    args = parser.parse_args(argv)
    setup_logging()
    body = args.body
    if args.body_file and args.body_file.is_file():
        body = (body + "\n\n" + args.body_file.read_text()).strip()
    print(notify(args.event, args.subject, body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

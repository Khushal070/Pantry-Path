"""Run summary: one message at the end of a successful batch run.

Collects the headline numbers that the other steps saved in reports/ (rows, dishes,
data-quality warnings, drift, bias, catalog evaluation, LLM usage) and sends them as an
info alert: always logged, and emailed when email alerts are configured.
"""

import json
import logging
from pathlib import Path
from typing import Any

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config

log = logging.getLogger(__name__)


def read_json(path: Path) -> Any:
    """The parsed file, or None when it is missing or unreadable (a summary never fails)."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def quality_warnings(results_dir: Path) -> int:
    """Failed warning-level expectations across the saved Great Expectations results."""
    total = 0
    for path in sorted(results_dir.glob("*.json")):
        report = read_json(path) or {}
        total += int(report.get("warnings") or 0)
    return total


def bias_headline(report: Any) -> str | None:
    """The bias report's one-line takeaway, if it has one."""
    if not isinstance(report, dict):
        return None
    for value in (report.get("headline"), report.get("summary")):
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("headline"), str):
            return value["headline"]
    return None


def catalog_eval_line(report: Any) -> str | None:
    """Ingredient recall per split, e.g. 'dev 91.1%, test 89.8% recall (ok)'."""
    if not isinstance(report, dict) or not isinstance(report.get("splits"), dict):
        return None
    parts = [f"{name} {s['recall']:.1%}" for name, s in report["splits"].items()
             if isinstance(s, dict) and isinstance(s.get("recall"), int | float)]
    return f"{', '.join(parts)} recall ({report.get('status')})" if parts else None


def llm_totals(usage: Any) -> dict[str, float]:
    """Numeric totals from the LLM usage report (calls, tokens, cost)."""
    if not isinstance(usage, dict):
        return {}
    totals = usage.get("totals") or usage.get("total") or usage
    if not isinstance(totals, dict):
        return {}
    return {k: v for k, v in totals.items()
            if isinstance(v, int | float) and not isinstance(v, bool)}


def run(cfg: Config, **_: Any) -> dict[str, Any]:
    """Send the success summary of a batch run."""
    reports = cfg.path(cfg["paths"]["reports"])
    stats = read_json(cfg.path(cfg["stats"]["output"])) or {}
    drift = read_json(cfg.path(cfg["drift"]["output"])) or {}
    summary = {
        "off_rows": stats.get("off", {}).get("rows"),
        "dishes": stats.get("catalog", {}).get("dishes"),
        "cuisines": stats.get("catalog", {}).get("cuisine"),
        "quality_warnings": quality_warnings(cfg.path(cfg["validation"]["results_dir"])),
        "outliers": sum((stats.get("outliers") or {}).values()),
        "drift": drift.get("status", "not run"),
        "drift_warnings": sum(c.get("status") == "warning" for c in drift.get("checks", [])),
        "bias": bias_headline(read_json(cfg.path(cfg["bias"]["output_json"]))),
        "catalog_eval": catalog_eval_line(
            read_json(cfg.path(cfg["catalog_eval"]["output_json"]))),
        "llm_usage": llm_totals(read_json(cfg.path(cfg["llm"]["usage_report"]))),
    }

    lines = [
        f"Open Food Facts products: {summary['off_rows']}",
        f"Dishes in the catalog: {summary['dishes']} {summary['cuisines'] or ''}".rstrip(),
        f"Data quality warnings: {summary['quality_warnings']}",
        f"Outliers flagged: {summary['outliers']}",
        f"Drift: {summary['drift']} ({summary['drift_warnings']} warning(s))",
    ]
    if summary["bias"]:
        lines.append(f"Bias: {summary['bias']}")
    if summary["catalog_eval"]:
        lines.append(f"Catalog evaluation: {summary['catalog_eval']}")
    if summary["llm_usage"]:
        lines.append("LLM usage: " + ", ".join(f"{k}={v}" for k, v in
                                                summary["llm_usage"].items()))
    lines.append(f"Reports: {reports}")
    send_alert("pipeline run succeeded", "\n".join(lines), level="info", cfg=cfg)
    return summary

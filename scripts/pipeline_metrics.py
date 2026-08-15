"""Small read-only metrics adapters shared by pipeline entry points."""

import json
from pathlib import Path


def quality_metrics(folder):
    path = Path(folder) / "quality_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "passed": bool(report.get("passed")),
        "error_count": len(report.get("errors", [])),
        "warning_count": len(report.get("warnings", [])),
        "error_codes": [
            item.get("code")
            for item in report.get("error_details", [])
            if isinstance(item, dict) and item.get("code")
        ],
        "claim_coverage": report.get("coverage", {}).get("claim_coverage"),
        "notes_claim_coverage": report.get(
            "coverage", {}).get("notes_claim_coverage"),
    }

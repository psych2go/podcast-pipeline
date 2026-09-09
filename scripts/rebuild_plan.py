"""Read-only reporting adapter for the content pipeline's rebuild decision.

There is only one freshness check: agent_pipeline.content_pipeline_needed.
Stages are conservative downstream candidates, not a targeted execution plan.
The existing report shape is retained for historical run-report consumers;
empty affected-unit/chapter lists mean unknown, not proof of no impact.
"""
from pathlib import Path

from scripts.agent_pipeline import content_pipeline_needed
from scripts.content_map import body_sha256


def current_transcript_basis(folder):
    folder = Path(folder)
    corrected = folder / "转录_纠错.txt"
    path = corrected if corrected.exists() else folder / "原始转录.txt"
    if not path.exists():
        return None
    return {
        "file": path.name,
        "sha256": body_sha256(path.read_text(encoding="utf-8")),
    }


def build_rebuild_plan(folder, *, force=False):
    """Report the same decision used by execution, without a second validator."""
    folder = Path(folder)
    needs_content = content_pipeline_needed(folder, force=force)
    return {
        "schema_version": 2,
        "mode": "active",
        "needs_content": needs_content,
        "reasons": (
            ["force_rebuild" if force else "deterministic_validation"]
            if needs_content else []
        ),
        "stages": [
            "transcript_correction", "content_map", "claim_evidence",
            "canonical_entities", "prewrite_fact_checks", "content_writing",
            "finalize", "ai_review", "tts", "html",
        ] if needs_content else [],
        "affected_units": [],
        "affected_chapters": [],
        "transcript_basis": current_transcript_basis(folder),
    }

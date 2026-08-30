"""Conservative rebuild planning for pipeline observability.

The planner is intentionally read-only. Execution remains owned by process.py;
this module records why content is stale and which stages would be affected so
targeted execution can be enabled after shadow-mode calibration.
"""
from pathlib import Path

try:
    from content_map import (
        body_sha256, load_json, validate_content_map, validate_summary_map,
    )
    from content_finalizer import validate_tts_readiness
    from agent_pipeline import _writing_inputs_are_current
    from prewrite_fact_checks import ledger_is_current
    from tts import load_tts_lexicon
except ImportError:
    from scripts.content_map import (
        body_sha256, load_json, validate_content_map, validate_summary_map,
    )
    from scripts.content_finalizer import validate_tts_readiness
    from scripts.agent_pipeline import _writing_inputs_are_current
    from scripts.prewrite_fact_checks import ledger_is_current
    from scripts.tts import load_tts_lexicon


CONTENT_ARTIFACTS = (
    "transcript.raw.json",
    "原始转录.txt",
    "content_map.json",
    "中文完整笔记.md",
    "讲书稿.md",
    "summary_map.json",
)


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
    """Return a JSON-serializable shadow plan without mutating artifacts."""
    folder = Path(folder)
    missing = [name for name in CONTENT_ARTIFACTS if not (folder / name).exists()]
    reasons = []
    stages = []
    affected_units = []
    affected_chapters = []

    if force:
        reasons.append("force_rebuild")
        stages = [
            "transcript_correction", "content_map", "claim_evidence",
            "content_writing", "finalize", "ai_review", "tts", "html",
        ]
    elif missing:
        reasons.extend(f"missing:{name}" for name in missing)
        if "content_map.json" in missing:
            stages.extend(["content_map", "claim_evidence"])
        stages.extend(["content_writing", "finalize", "ai_review", "tts", "html"])
    else:
        try:
            summary = load_json(folder / "summary_map.json")
            transcript = load_json(folder / "transcript.raw.json")
            content_map = load_json(folder / "content_map.json")
            notes_text = (folder / "中文完整笔记.md").read_text(
                encoding="utf-8")
            briefing_text = (folder / "讲书稿.md").read_text(
                encoding="utf-8")
        except (OSError, ValueError, TypeError):
            summary = {}
            transcript = None
            content_map = None
            notes_text = ""
            briefing_text = ""
            reasons.append("invalid:content_artifacts")
            stages.extend([
                "content_map", "claim_evidence", "content_writing",
                "finalize", "ai_review", "tts", "html",
            ])
        basis = current_transcript_basis(folder)
        if summary.get("transcript_basis") != basis:
            reasons.append("stale:transcript_basis")
            stages.extend([
                "content_writing", "finalize", "ai_review", "tts", "html",
            ])
        if not _writing_inputs_are_current(folder, summary):
            reasons.append("stale:writing_inputs")
            stages.extend([
                "content_writing", "finalize", "ai_review", "tts", "html",
            ])
        if content_map is not None and transcript is not None:
            map_errors, _warnings = validate_content_map(
                content_map, transcript)
            if map_errors:
                reasons.append("invalid:content_map.json")
                stages.extend([
                    "claim_evidence", "content_writing", "finalize",
                    "ai_review", "tts", "html",
                ])
            summary_errors = validate_summary_map(
                summary, briefing_text, content_map, notes_text)
            if summary_errors:
                reasons.append("invalid:summary_map.json")
                stages.extend([
                    "content_writing", "finalize", "ai_review", "tts", "html",
                ])
            ledger_required = (
                int(content_map.get("prewrite_fact_checks_version", 0) or 0)
                >= 1
                or (folder / "editorial_fact_checks.json").exists()
            )
            if ledger_required and not ledger_is_current(folder):
                reasons.append("stale:editorial_fact_checks.json")
                stages.extend([
                    "prewrite_fact_checks", "content_writing", "finalize",
                    "ai_review", "tts", "html",
                ])
        tts_errors = validate_tts_readiness(
            briefing_text, load_tts_lexicon(folder))
        if tts_errors:
            reasons.append("invalid:tts_lexicon.json")
            stages.extend(["finalize", "ai_review", "tts", "html"])
        if "content_writing" in stages:
            for chapter in summary.get("chapters", []) or []:
                if not isinstance(chapter, dict):
                    continue
                affected_chapters.append(chapter.get("title"))
                affected_units.extend(chapter.get("unit_ids", []) or [])

    ordered_stages = list(dict.fromkeys(stages))
    return {
        "schema_version": 2,
        "mode": "active",
        "needs_content": bool(reasons),
        "reasons": reasons,
        "stages": ordered_stages,
        "affected_units": sorted(set(filter(None, affected_units))),
        "affected_chapters": list(dict.fromkeys(filter(None, affected_chapters))),
        "transcript_basis": current_transcript_basis(folder),
    }

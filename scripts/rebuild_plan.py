"""Conservative rebuild planning for pipeline observability.

The planner is intentionally read-only. Execution remains owned by process.py;
this module records why content is stale and which stages would be affected so
targeted execution can be enabled after shadow-mode calibration.
"""
from pathlib import Path

try:
    from content_map import body_sha256, load_json
except ImportError:
    from scripts.content_map import body_sha256, load_json


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
        except (OSError, ValueError, TypeError):
            summary = {}
            reasons.append("invalid:summary_map.json")
        basis = current_transcript_basis(folder)
        if summary.get("transcript_basis") != basis:
            reasons.append("stale:transcript_basis")
            stages.extend(["content_writing", "finalize", "ai_review", "tts", "html"])
        for chapter in summary.get("chapters", []) or []:
            if not isinstance(chapter, dict):
                continue
            affected_chapters.append(chapter.get("title"))
            affected_units.extend(chapter.get("unit_ids", []) or [])

    ordered_stages = list(dict.fromkeys(stages))
    return {
        "schema_version": 1,
        "mode": "shadow",
        "needs_content": bool(reasons),
        "reasons": reasons,
        "stages": ordered_stages,
        "affected_units": sorted(set(filter(None, affected_units))),
        "affected_chapters": list(dict.fromkeys(filter(None, affected_chapters))),
        "transcript_basis": current_transcript_basis(folder),
    }

"""Bounded review/repair loop that never weakens review thresholds."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from ai_review import review_episode
    from atomic_io import atomic_write_json
    from claim_evidence import refine_claim_evidence
    from content_finalizer import (
        finalize_content_package,
        generate_safe_tts_lexicon,
        validate_tts_readiness,
    )
    from tts import load_tts_lexicon
except ImportError:
    from scripts.ai_review import review_episode
    from scripts.atomic_io import atomic_write_json
    from scripts.claim_evidence import refine_claim_evidence
    from scripts.content_finalizer import (
        finalize_content_package,
        generate_safe_tts_lexicon,
        validate_tts_readiness,
    )
    from scripts.tts import load_tts_lexicon


SAFE_SUMMARY_CATEGORIES = {"summary_map", "summary", "coverage_mapping"}
SAFE_TTS_CATEGORIES = {"tts", "tts_lexicon", "pronunciation"}
SAFE_EVIDENCE_CATEGORIES = {"evidence_integrity", "claim_evidence"}
BLOCKED_CATEGORIES = {
    "factuality", "medical", "health", "attribution", "numbers",
    "transcript_quality", "entity_accuracy",
}


def _category(issue):
    return str(issue.get("category", "")).strip().casefold()


def _issue_text(issue):
    return " ".join(str(issue.get(key, "")) for key in (
        "statement", "source_evidence", "recommendation", "file"))


def _repair_summary(folder):
    result = finalize_content_package(folder)
    return {
        "action": "finalize_content_package",
        "normalization_changes": result.get("normalization_changes", []),
    }


def _repair_tts(folder):
    folder = Path(folder)
    briefing = (folder / "讲书稿.md").read_text(encoding="utf-8")
    existing = load_tts_lexicon(folder)
    generated = generate_safe_tts_lexicon(briefing, existing)
    if generated != existing:
        atomic_write_json(folder / "tts_lexicon.json", generated)
    unresolved = validate_tts_readiness(briefing, generated)
    if unresolved:
        raise RuntimeError(
            "安全 TTS 自动修复后仍有未确认读音: " + "; ".join(unresolved))
    return {
        "action": "refresh_safe_tts_lexicon",
        "entries": len(generated),
    }


def _repair_evidence(folder, issues):
    unit_ids = sorted(set(re.findall(
        r"\bU\d{4}\b", " ".join(_issue_text(issue) for issue in issues))))
    if not unit_ids:
        raise RuntimeError("evidence_integrity issue 未明确指出 unit ID，禁止猜测重做范围")
    metrics = refine_claim_evidence(folder, unit_ids=unit_ids)
    return {
        "action": "refine_claim_evidence",
        "unit_ids": unit_ids,
        "claim_count": metrics.get("claim_count", 0),
    }


def repair_safe_issues(folder, review):
    """Apply deterministic repairs only; return actions and blocking reasons."""
    issues = [
        issue for issue in review.get("issues", []) or []
        if isinstance(issue, dict)
    ]
    high_issues = [
        issue for issue in issues
        if issue.get("severity") in {"critical", "high"}
    ]
    blocked = [
        issue for issue in high_issues
        if _category(issue) in BLOCKED_CATEGORIES
    ]
    unknown = [
        issue for issue in high_issues
        if _category(issue) not in (
            SAFE_SUMMARY_CATEGORIES
            | SAFE_TTS_CATEGORIES
            | SAFE_EVIDENCE_CATEGORIES
            | BLOCKED_CATEGORIES
        )
    ]
    if blocked or unknown:
        categories = sorted({_category(issue) or "unknown" for issue in blocked + unknown})
        return [], [
            "以下 high/critical 类别禁止无证据自动修复: " + ", ".join(categories)
        ]

    actions = []
    categories = {_category(issue) for issue in high_issues}
    if categories & SAFE_SUMMARY_CATEGORIES:
        actions.append(_repair_summary(folder))
    if categories & SAFE_TTS_CATEGORIES:
        actions.append(_repair_tts(folder))
    evidence_issues = [
        issue for issue in high_issues
        if _category(issue) in SAFE_EVIDENCE_CATEGORIES
    ]
    if evidence_issues:
        actions.append(_repair_evidence(folder, evidence_issues))
    if not actions:
        return [], ["审查未通过，但没有可安全自动修复的 high/critical issue"]
    return actions, []


def review_and_repair(
        folder, *, max_rounds=2, reviewer=None,
        model="", effort="max", run_report=None):
    """Review, perform bounded safe repairs, and independently re-review."""
    folder = Path(folder)
    if reviewer is None:
        try:
            import ai_review
        except ImportError:
            from scripts import ai_review
        reviewer = ai_review.review_episode
    history = []
    review = None
    for round_index in range(max_rounds + 1):
        review_kwargs = {"model": model, "effort": effort}
        if run_report is not None:
            review_kwargs["run_report"] = run_report
        review = reviewer(folder, **review_kwargs)
        if not isinstance(review, dict):
            # Compatibility with injected/legacy reviewers that only perform
            # side effects. The caller's deterministic quality report remains
            # the source of truth and will decide whether publication passes.
            return review
        history.append({
            "round": round_index,
            "reviewed_at": review.get("reviewed_at"),
            "passed": bool(review.get("passed")),
            "reviewed_files": review.get("reviewed_files", {}),
        })
        if review.get("passed"):
            break
        if round_index >= max_rounds:
            break
        try:
            actions, blockers = repair_safe_issues(folder, review)
        except Exception as exc:
            actions, blockers = [], [str(exc)]
        history[-1]["repair_actions"] = actions
        history[-1]["repair_blockers"] = blockers
        if blockers or not actions:
            break

    payload = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "max_rounds": max_rounds,
        "passed": bool(review and review.get("passed")),
        "history": history,
    }
    atomic_write_json(folder / "review_repair.json", payload)
    return review

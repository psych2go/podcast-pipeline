"""Deterministic attribution of AI review rejections.

Every caller (run-report stage metrics, review-repair history, catalog
health aggregation, and triage) shares this single taxonomy so rejection
reasons stay comparable across episodes and over time.  Attribution is a
pure projection of the review JSON: no model call, no threshold change, and
no mutation of review verdicts.
"""
from collections import Counter

try:
    from quality_errors import (
        AI_REVIEW_SCORE,
        AI_REVIEW_SECTION,
        AI_REVIEW_SEVERE_ISSUE,
    )
except ImportError:
    from scripts.quality_errors import (
        AI_REVIEW_SCORE,
        AI_REVIEW_SECTION,
        AI_REVIEW_SEVERE_ISSUE,
)


# Sections carrying a 0-100 score; mirrors the strict quality gate.
SCORING_SECTIONS = ("transcript_quality", "coverage", "factuality")
# Pass/fail-only sections from REVIEW_SCHEMA.
PASSFAIL_SECTIONS = ("numbers", "attribution", "entity_accuracy", "tts", "publish")
SCORE_THRESHOLD = 90

BLOCKING_SEVERITIES = frozenset({"critical", "high"})
MAX_ATTRIBUTED_ISSUES = 20
STATEMENT_HEAD_CHARS = 120

# Issue categories are model-invented free text.  Exact aliases cover every
# raw value observed in retained failure snapshots plus the category sets
# already codified in review_repair.py.  New raw values fall through prefix
# matching and finally "other"; the raw text is always retained so the alias
# table can be extended deliberately.
CATEGORY_ALIASES = {
    # Backstage narration leakage in published prose.
    "briefing_style": "audit_narration",
    "notes_style": "audit_narration",
    "backstage_narration": "audit_narration",
    "audit_narration": "audit_narration",
    # Entity accuracy.
    "entity_accuracy": "entity",
    "entity": "entity",
    "transcript_title_normalization": "entity",
    # Numbers.
    "numbers": "numbers",
    "numbers_geography_mismatch": "numbers",
    "numbers_scope_misattribution": "numbers",
    "number_context_missing": "numbers",
    "dynamic_policy_number": "numbers",
    "dynamic_number": "numbers",
    # Factuality.
    "factuality": "factuality",
    "dynamic_context": "factuality",
    "medical": "factuality",
    "health": "factuality",
    # Attribution.
    "attribution": "attribution",
    "personal_relationship_attribution": "attribution",
    # Transcript quality / traceability.
    "transcript_quality": "transcript",
    "transcript": "transcript",
    "transcript_traceability": "transcript",
    # Coverage.
    "coverage": "coverage",
    "coverage_mapping": "coverage",
    # Summary map mapping/hash integrity.
    "summary_map": "summary_map",
    "summary": "summary_map",
    "coverage_summary_map_mismatch": "summary_map",
    # TTS readability and lexicon.
    "tts": "tts",
    "tts_lexicon": "tts",
    "pronunciation": "tts",
    "tts_readability": "tts",
    "tts_substring_collision": "tts",
    "tts_lexicon_coverage": "tts",
    # Evidence bindings and content-map evidence warnings.
    "evidence_integrity": "evidence_integrity",
    "claim_evidence": "evidence_integrity",
    "content_map_evidence_warning": "evidence_integrity",
    # Fact-check / editorial ledger contract violations.
    "fact_check_ledger_schema": "fact_check_contract",
    "allegation_source_document_missing": "fact_check_contract",
}

CATEGORY_PREFIXES = (
    ("audit_", "audit_narration"),
    ("briefing_", "audit_narration"),
    ("notes_style", "audit_narration"),
    ("entity_", "entity"),
    ("number", "numbers"),
    ("dynamic_", "factuality"),
    ("factuality", "factuality"),
    ("medical", "factuality"),
    ("health", "factuality"),
    ("attribution", "attribution"),
    ("transcript", "transcript"),
    ("coverage", "coverage"),
    ("summary", "summary_map"),
    ("tts", "tts"),
    ("pronunciation", "tts"),
    ("claim_evidence", "evidence_integrity"),
    ("evidence", "evidence_integrity"),
    ("content_map", "evidence_integrity"),
    ("fact_check", "fact_check_contract"),
    ("allegation", "fact_check_contract"),
)

OTHER_CATEGORY = "other"


def normalize_category(raw):
    """Map a model-invented issue category onto the stable taxonomy."""
    key = str(raw or "").strip().casefold()
    if not key:
        return OTHER_CATEGORY
    exact = CATEGORY_ALIASES.get(key)
    if exact:
        return exact
    for prefix, normalized in CATEGORY_PREFIXES:
        if key.startswith(prefix):
            return normalized
    return OTHER_CATEGORY


def _section_score(review, name):
    section = review.get(name)
    if not isinstance(section, dict):
        return None
    score = section.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return score
    return None


def _failed_sections(review):
    failed = []
    for name in SCORING_SECTIONS + PASSFAIL_SECTIONS:
        section = review.get(name)
        if isinstance(section, dict) and section.get("passed") is False:
            failed.append(name)
    return failed


def _blocking_issues(review):
    issues = review.get("issues")
    if not isinstance(issues, list):
        return []
    return [
        issue for issue in issues
        if isinstance(issue, dict)
        and str(issue.get("severity", "")).strip().lower() in BLOCKING_SEVERITIES
    ]


def attribute_rejection(review):
    """Project a failed review onto bounded, machine-aggregable fields.

    The result is safe to embed in run-report stage metrics: issue detail is
    limited to blocking severities, truncated, and capped.  Category counts
    cover blocking issues only, because those drive the rejection verdict.
    """
    review = review if isinstance(review, dict) else {}
    blocking = _blocking_issues(review)
    sections_failed = _failed_sections(review)
    scores = {
        name: _section_score(review, name)
        for name in SCORING_SECTIONS
    }
    low_score_sections = sorted(
        name for name, score in scores.items()
        if score is not None and score < SCORE_THRESHOLD
    )

    codes = []
    if blocking:
        codes.append(AI_REVIEW_SEVERE_ISSUE)
    if sections_failed:
        codes.append(AI_REVIEW_SECTION)
    if low_score_sections:
        codes.append(AI_REVIEW_SCORE)

    category_counts = Counter()
    other_raw = Counter()
    attributed = []
    for issue in blocking[:MAX_ATTRIBUTED_ISSUES]:
        raw = str(issue.get("category", "")).strip()
        normalized = normalize_category(raw)
        category_counts[normalized] += 1
        if normalized == OTHER_CATEGORY and raw:
            other_raw[raw] += 1
        statement = str(issue.get("statement", "") or "")
        attributed.append({
            "severity": str(issue.get("severity", "")).strip().lower(),
            "category": raw,
            "normalized": normalized,
            "file": str(issue.get("file", "") or ""),
            "repair_kind": str(issue.get("repair_kind", "") or ""),
            "statement_head": statement[:STATEMENT_HEAD_CHARS],
        })
    # Issues beyond the cap still contribute to the aggregate counts so
    # category totals stay meaningful for long inventories.
    for issue in blocking[MAX_ATTRIBUTED_ISSUES:]:
        raw = str(issue.get("category", "")).strip()
        normalized = normalize_category(raw)
        category_counts[normalized] += 1
        if normalized == OTHER_CATEGORY and raw:
            other_raw[raw] += 1

    return {
        "codes": codes,
        "sections_failed": sections_failed,
        "scores": scores,
        "issue_counts": dict(category_counts),
        "other_raw_counts": dict(other_raw),
        "issues": attributed,
    }

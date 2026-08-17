"""Claim provenance, assertion shape, and compatibility rules for AI review."""
import re


AI_REVIEW_SCHEMA_VERSION = 3

LEGACY_CLAIM_TYPES = (
    "public_fact",
    "guest_firsthand",
    "guest_opinion",
    "editorial_fact",
    "editorial_inference",
    "not_applicable",
)

CLAIM_ORIGINS = (
    "speaker_firsthand",
    "speaker_reported",
    "external_source",
    "editorial_added",
    "episode_metadata",
)

SPEAKER_ROLES = (
    "guest",
    "host",
    "quoted_third_party",
    "editorial",
    "not_applicable",
    "unknown",
)

ASSERTION_TYPES = (
    "fact",
    "opinion",
    "prediction",
    "recommendation",
    "explanation",
    "definition",
    "anecdote",
    "allegation",
    "inference",
)

VERIFICATION_MODES = (
    "web_required",
    "source_document_required",
    "web_spot_check",
    "transcript_attribution",
    "transcript_only",
    "safety_cross_check",
    "not_applicable",
)

RISK_DOMAINS = (
    "general",
    "medical",
    "legal",
    "financial",
    "political",
    "safety",
)

VERDICTS = (
    "supported",
    "qualified",
    "unsupported",
    "contradicted",
    "faithfully_attributed",
    "accurately_reported",
    "not_applicable",
    "uncertain",
)

PUBLICATION_STATUSES = (
    "used_as_fact",
    "attributed_or_qualified",
    "excluded",
)


_ATOMIC_SUBCLAIM_RE = re.compile(r"^(U\d{4}-C\d{2})-F(\d{2})$")


def derive_legacy_claim_type(item):
    """Derive the v2 compatibility bucket from the orthogonal v3 fields."""
    origin = item.get("claim_origin")
    role = item.get("speaker_role")
    assertion = item.get("assertion_type")
    if origin == "speaker_firsthand" and role == "guest" and assertion in {
            "fact", "anecdote"}:
        return "guest_firsthand"
    if role == "guest" and assertion in {
            "opinion", "prediction", "recommendation"}:
        return "guest_opinion"
    if origin == "external_source" and assertion == "fact":
        return "public_fact"
    if origin == "editorial_added" and assertion == "fact":
        return "editorial_fact"
    if origin == "editorial_added" and assertion == "inference":
        return "editorial_inference"
    return "not_applicable"


def atomic_subclaim_parent(subclaim_id):
    match = _ATOMIC_SUBCLAIM_RE.fullmatch(str(subclaim_id or ""))
    return match.group(1) if match else None


def is_cacheable_fact_check(item):
    """Only externally checkable objective facts belong in the web cache."""
    if "claim_origin" in item or "assertion_type" in item:
        if item.get("assertion_type") != "fact":
            return False
        return item.get("claim_origin") in {
            "external_source", "editorial_added"}
    # AI review v2 compatibility during gradual re-review migration.
    return item.get("claim_type") in {"public_fact", "editorial_fact"}

def normalize_review_fact_checks(review):
    """Mechanically align compatibility fields with orthogonal v3 semantics.

    For speaker_reported facts, ``verdict`` evaluates whether the program's
    report is faithfully represented. Any external disagreement remains in
    notes/source_urls rather than turning the attributed speech report itself
    into an unsupported editorial fact.
    """
    changes = []
    for item in review.get("fact_checks", []) or []:
        if not isinstance(item, dict):
            continue
        subclaim = item.get("subclaim_id", "")
        expected_type = derive_legacy_claim_type(item)
        if item.get("claim_type") != expected_type:
            changes.append({
                "subclaim_id": subclaim,
                "field": "claim_type",
                "before": item.get("claim_type"),
                "after": expected_type,
            })
            item["claim_type"] = expected_type

        origin = item.get("claim_origin")
        assertion = item.get("assertion_type")
        status = item.get("publication_status")
        verdict = item.get("verdict")
        segments = item.get("evidence_segment_ids") or []
        if (
                origin == "speaker_reported"
                and assertion == "fact"
                and status == "attributed_or_qualified"
                and segments
                and verdict in {
                    "supported", "unsupported", "contradicted",
                    "faithfully_attributed", "not_applicable",
                }):
            changes.append({
                "subclaim_id": subclaim,
                "field": "verdict",
                "before": verdict,
                "after": "accurately_reported",
            })
            item["verdict"] = "accurately_reported"
        elif (
                origin == "speaker_firsthand"
                and status == "attributed_or_qualified"
                and segments
                and verdict == "supported"):
            changes.append({
                "subclaim_id": subclaim,
                "field": "verdict",
                "before": verdict,
                "after": "faithfully_attributed",
            })
            item["verdict"] = "faithfully_attributed"
    return changes

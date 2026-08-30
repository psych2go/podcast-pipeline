"""Stable quality error codes emitted at the check that detects each failure."""

ASR_QUALITY_FAILED = "asr_quality_failed"
ASR_COMPLETENESS_MISSING = "asr_completeness_missing"
ASR_SPEECH_COVERAGE_FAILED = "asr_speech_coverage_failed"
ASR_TIMELINE_INVALID = "asr_timeline_invalid"
TRANSCRIPT_MISSING = "transcript_missing"
TRANSCRIPT_INTEGRITY_FAILED = "transcript_integrity_failed"
TRANSCRIPT_CORRECTION_MISSING = "transcript_correction_missing"
CORRECTION_MANIFEST_MISSING = "correction_manifest_missing"
CORRECTION_MANIFEST_INVALID = "correction_manifest_invalid"
CORRECTION_UNRESOLVED_HIGH_RISK = "correction_unresolved_high_risk"
CONTENT_MAP_MISSING = "content_map_missing"
CONTENT_MAP_MODE_MISMATCH = "content_map_evidence_mode_mismatch"
CONTENT_MAP_SOURCE_SEGMENT_MISSING = "content_map_source_segment_missing"
CONTENT_MAP_EXCLUSION_INVALID = "content_map_exclusion_invalid"
CLAIM_EVIDENCE_FALLBACK = "claim_evidence_fallback"
PREWRITE_FACT_CHECKS_INVALID = "prewrite_fact_checks_invalid"
SUMMARY_MAP_SCHEMA = "summary_map_schema_outdated"
SUMMARY_MAP_MISSING = "summary_map_missing"
COVERAGE_FAILED = "summary_coverage_failed"
BRIEFING_STRUCTURE_FAILED = "briefing_structure_failed"
BRIEFING_AUDIT_NARRATION = "briefing_audit_narration"
BRIEFING_MISSING = "briefing_missing"
NOTES_MISSING = "complete_notes_missing"
NOTES_AUDIT_NARRATION = "notes_audit_narration"
SOURCE_QUALITY_FAILED = "source_quality_failed"
AI_REVIEW_MISSING = "ai_review_missing"
AI_REVIEW_STALE = "ai_review_stale"
AI_REVIEW_FAILED = "ai_review_failed"
AI_REVIEW_SECTION = "ai_review_section_failed"
AI_REVIEW_SCORE = "ai_review_score_below_threshold"
AI_REVIEW_SEVERE_ISSUE = "ai_review_severe_issue"
AI_REVIEW_FACT_CHECK = "ai_review_fact_check_invalid"
AI_REVIEW_INCOMPLETE = "ai_review_incomplete"
AI_REVIEW_ISSUE_EVIDENCE = "ai_review_issue_evidence_incomplete"
ENTITY_ACCURACY_FAILED = "entity_accuracy_failed"
SOURCE_REVIEW_STATUS = "source_review_status"
CONTENT_REVIEW_STATUS = "content_review_status"
EVIDENCE_PROVENANCE_FAILED = "evidence_provenance_failed"
CONTENT_MAP_SCHEMA = "content_map_schema_outdated"
CONTENT_MAP_VALIDATION = "content_map_validation_failed"
SUMMARY_MAP_VALIDATION = "summary_map_validation_failed"
TTS_READINESS_FAILED = "tts_readiness_failed"
QUALITY_VALIDATION_FAILED = "quality_validation_failed"

AUTO_REVIEW_CODES = frozenset({
    AI_REVIEW_MISSING,
    AI_REVIEW_STALE,
    SOURCE_REVIEW_STATUS,
    CONTENT_REVIEW_STATUS,
})


class CodedErrors(list):
    """String-compatible error list that records codes at append time."""

    def __init__(self, details):
        super().__init__()
        self._details = details

    def append(self, message, *, code=QUALITY_VALIDATION_FAILED):
        text = str(message)
        super().append(text)
        self._details.append({"code": str(code), "message": text})

    def extend(self, messages, *, code=QUALITY_VALIDATION_FAILED):
        for message in messages:
            self.append(message, code=code)


def coded_errors(report):
    existing = list(report.get("errors", []))
    details = []
    report["error_details"] = details
    errors = CodedErrors(details)
    for message in existing:
        errors.append(message)
    report["errors"] = errors
    return errors


def quality_error_alignment(report):
    errors = [str(message) for message in report.get("errors", [])]
    details = report.get("error_details")
    if not isinstance(details, list):
        return False
    messages = [
        str(item.get("message"))
        for item in details if isinstance(item, dict)
    ]
    return len(messages) == len(details) and messages == errors


def add_error(report, code, message):
    errors = report.get("errors")
    if not isinstance(errors, CodedErrors):
        errors = coded_errors(report)
    errors.append(message, code=code)


def extend_errors(report, code, messages):
    for message in messages:
        add_error(report, code, message)

"""Adaptive ASR context building and difficult-segment refinement."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import fmean
from typing import Callable, Iterable, Sequence

try:
    from hashing import sha256_text as _text_sha256
except ImportError:
    from scripts.hashing import sha256_text as _text_sha256


_GENERIC_WORDS = frozenset({
    "a", "an", "and", "at", "episode", "for", "from", "full", "how",
    "in", "into", "live", "of", "on", "or", "podcast", "show", "the",
    "this", "to", "transcript", "what", "when", "where", "why", "with",
})
_NUMBER_OR_URL = re.compile(
    r"(?:https?://|www\.)|(?<![A-Za-z])\$?\d+(?:[,.]\d+)*(?:%|x|k|m|b)?",
    re.IGNORECASE,
)
_CAPITALIZED_SEQUENCE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'&.-]*"
    r"(?:\s+(?:[a-z]{1,3}\s+)?)){1,4}[A-Z][A-Za-z0-9'&.-]*\b"
)
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9&.-]{1,}\b")
_CAMEL_CASE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b")


@dataclass(frozen=True)
class AsrContext:
    """Compact context passed to first-pass and range decoders."""

    initial_prompt: str | None
    hotwords: str | None
    terms: tuple[str, ...]
    sources: tuple[str, ...]

    def to_metadata(self) -> dict:
        return {
            "terms": list(self.terms),
            "sources": list(self.sources),
            "initial_prompt": self.initial_prompt,
            "hotwords": self.hotwords,
        }


@dataclass(frozen=True)
class SegmentAssessment:
    index: int
    risk: float
    reasons: tuple[str, ...]
    critical_content: bool

    @property
    def needs_redecode(self) -> bool:
        return self.risk >= 1.75


@dataclass(frozen=True)
class RefinementRange:
    start_index: int
    end_index: int
    start: float
    end: float
    risk: float
    reasons: tuple[str, ...]


RangeDecoder = Callable[[float, float, AsrContext], Sequence[dict]]


def _normalize_term(value):
    value = re.sub(r"\s+", " ", (value or "")).strip(" \t\r\n,;:|")
    return value


def _term_key(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _useful_term(value):
    value = _normalize_term(value)
    if not value or len(value) < 2 or len(value) > 100:
        return False
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.-]*", value)
    if not words or len(words) > 10:
        return False
    return any(word.lower() not in _GENERIC_WORDS for word in words)


def _split_manual_hotwords(value):
    if not value:
        return []
    return [
        item for item in
        (_normalize_term(part) for part in re.split(r"[,;\n]+", value))
        if _useful_term(item)
    ]


def _extract_terms(text, include_fragments=False):
    if not text:
        return []
    value = re.sub(r"https?://\S+", " ", text)
    value = re.sub(r"^[#>*\-\s]+", "", value, flags=re.MULTILINE)
    candidates = []
    if include_fragments:
        for fragment in re.split(r"\s*(?:\+|\||,|;|:|/|[–—])\s*", value):
            fragment = _normalize_term(fragment)
            word_count = len(re.findall(r"[A-Za-z0-9]+", fragment))
            if 2 <= word_count <= 10 and _useful_term(fragment):
                candidates.append(fragment)
    candidates.extend(match.group(0) for match in _CAPITALIZED_SEQUENCE.finditer(value))
    candidates.extend(match.group(0) for match in _ACRONYM.finditer(value))
    candidates.extend(match.group(0) for match in _CAMEL_CASE.finditer(value))
    return [_normalize_term(item) for item in candidates if _useful_term(item)]


def build_asr_context(
        title="",
        context_texts: Iterable[str] = (),
        initial_prompt=None,
        hotwords=None,
        max_terms=24,
        max_hotword_chars=480,
):
    """Build bounded prompt/hotwords from explicit and episode context."""
    ordered = []
    sources = []
    seen = set()

    def add(values, source):
        added = False
        for value in values:
            key = _term_key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(value)
            added = True
            if len(ordered) >= max_terms:
                break
        if added and source not in sources:
            sources.append(source)

    add(_split_manual_hotwords(hotwords), "manual_hotwords")
    add(_extract_terms(title, include_fragments=True), "title")
    for index, text in enumerate(context_texts, start=1):
        if len(ordered) >= max_terms:
            break
        add(_extract_terms(text, include_fragments=False), f"context_{index}")

    bounded = []
    used_chars = 0
    for term in ordered[:max_terms]:
        extra = len(term) + (2 if bounded else 0)
        if bounded and used_chars + extra > max_hotword_chars:
            break
        bounded.append(term)
        used_chars += extra

    prompt = _normalize_term(initial_prompt) if initial_prompt else None
    clean_title = _normalize_term(re.sub(r'[\\/:*?"<>|\[\]$`]', " ", title))
    if not prompt and clean_title and len(clean_title) >= 10:
        prompt = f"Podcast about {clean_title}."
    prompt_terms = bounded[:12]
    if prompt_terms:
        suffix = "Known names and terms: " + ", ".join(prompt_terms) + "."
        prompt = f"{prompt} {suffix}".strip() if prompt else suffix

    return AsrContext(
        initial_prompt=prompt,
        hotwords=", ".join(bounded) if bounded else None,
        terms=tuple(bounded),
        sources=tuple(sources),
    )


def _mean_numeric(values, default):
    numeric = [
        float(value) for value in values
        if isinstance(value, (int, float))
    ]
    return fmean(numeric) if numeric else default


def assess_segment(segment, index=0, context_terms=()):
    """Return deterministic risk signals for one ASR segment."""
    text = (segment.get("text") or "").strip()
    reasons = []
    risk = 0.0

    logprob = segment.get("avg_logprob")
    if isinstance(logprob, (int, float)):
        if logprob < -1.5:
            reasons.append("very_low_logprob")
            risk += 2.0
        elif logprob < -1.0:
            reasons.append("low_logprob")
            risk += 1.0

    compression = segment.get("compression_ratio")
    if isinstance(compression, (int, float)):
        if compression > 2.4:
            reasons.append("very_high_compression")
            risk += 2.0
        elif compression > 1.8:
            reasons.append("high_compression")
            risk += 1.0

    no_speech = segment.get("no_speech_prob")
    if text and isinstance(no_speech, (int, float)):
        if no_speech > 0.6:
            reasons.append("speech_probability_conflict")
            risk += 2.0
        elif no_speech > 0.45:
            reasons.append("possible_speech_probability_conflict")
            risk += 1.0

    probabilities = [
        word.get("probability")
        for word in (segment.get("words") or [])
        if isinstance(word, dict)
        and isinstance(word.get("probability"), (int, float))
    ]
    if len(probabilities) >= 3:
        low_ratio = sum(value < 0.55 for value in probabilities) / len(probabilities)
        if low_ratio >= 0.7:
            reasons.append("many_low_probability_words")
            risk += 2.0
        elif low_ratio >= 0.35:
            reasons.append("low_probability_words")
            risk += 1.0

    if segment.get("speaker_alignment") == "unresolved" or segment.get("needs_review"):
        reasons.append("unresolved_alignment")
        risk += 2.0

    critical = bool(_NUMBER_OR_URL.search(text))
    lowered = text.lower()
    if not critical:
        critical = any(
            len(term) >= 3 and term.lower() in lowered
            for term in context_terms
        )
    if critical and risk > 0:
        reasons.append("critical_content")
        risk += 0.75

    temperature = segment.get("temperature")
    if risk > 0 and isinstance(temperature, (int, float)) and temperature > 0:
        reasons.append("temperature_fallback")
        risk += 0.25

    return SegmentAssessment(
        index=index,
        risk=round(risk, 3),
        reasons=tuple(dict.fromkeys(reasons)),
        critical_content=critical,
    )


def annotate_segments(segments, context_terms=()):
    """Copy segments and attach stable quality flags."""
    annotated = []
    assessments = []
    for index, original in enumerate(segments):
        segment = dict(original)
        assessment = assess_segment(segment, index, context_terms)
        segment["quality_flags"] = list(assessment.reasons)
        segment["needs_redecode"] = assessment.needs_redecode
        annotated.append(segment)
        assessments.append(assessment)
    return annotated, assessments


def build_refinement_ranges(
        segments,
        assessments,
        max_duration_seconds=45.0,
        max_ranges=8,
):
    """Merge nearby difficult segments and cap work by highest risk."""
    selected = [item for item in assessments if item.needs_redecode]
    ranges = []
    current = None
    for assessment in selected:
        segment = segments[assessment.index]
        start = segment.get("start")
        end = segment.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start, end = float(start), float(end)
        if end <= start:
            continue
        can_merge = (
            current is not None
            and assessment.index <= current["end_index"] + 2
            and start - current["end"] <= 1.5
            and end - current["start"] <= max_duration_seconds
        )
        if can_merge:
            current["end_index"] = assessment.index
            current["end"] = end
            current["risk"] = max(current["risk"], assessment.risk)
            current["reasons"].update(assessment.reasons)
            continue
        if current is not None:
            ranges.append(current)
        current = {
            "start_index": assessment.index,
            "end_index": assessment.index,
            "start": start,
            "end": end,
            "risk": assessment.risk,
            "reasons": set(assessment.reasons),
        }
    if current is not None:
        ranges.append(current)

    ranked = sorted(
        ranges,
        key=lambda item: (-item["risk"], item["start_index"]),
    )[:max_ranges]
    return [
        RefinementRange(
            start_index=item["start_index"],
            end_index=item["end_index"],
            start=item["start"],
            end=item["end"],
            risk=item["risk"],
            reasons=tuple(sorted(item["reasons"])),
        )
        for item in sorted(ranked, key=lambda item: item["start_index"])
    ], max(0, len(ranges) - len(ranked))


def _quality_score(segments):
    nonempty = [segment for segment in segments if (segment.get("text") or "").strip()]
    if not nonempty:
        return -10.0
    logprob = _mean_numeric(
        (segment.get("avg_logprob") for segment in nonempty), -1.25)
    compression = _mean_numeric(
        (segment.get("compression_ratio") for segment in nonempty), 1.5)
    no_speech = _mean_numeric(
        (segment.get("no_speech_prob") for segment in nonempty), 0.0)
    word_probability = _mean_numeric(
        (
            word.get("probability")
            for segment in nonempty
            for word in (segment.get("words") or [])
            if isinstance(word, dict)
        ),
        0.5,
    )
    return round(
        logprob
        + 0.75 * word_probability
        - 0.35 * max(0.0, compression - 1.5)
        - 0.5 * no_speech,
        4,
    )


def _render_text(segments):
    return " ".join(
        (segment.get("text") or "").strip()
        for segment in segments
        if (segment.get("text") or "").strip()
    ).strip()


def _text_tokens(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _looks_like_prompt_echo(text, context_terms):
    tokens = _text_tokens(text)
    if len(tokens) < 4:
        return False
    context_tokens = {
        token
        for term in context_terms
        for token in _text_tokens(term)
    }
    if not context_tokens:
        return False
    context_coverage = sum(
        token in context_tokens for token in tokens) / len(tokens)
    unique_ratio = len(set(tokens)) / len(tokens)
    return context_coverage >= 0.75 and unique_ratio <= 0.55


def _plausible_word_rate(segments):
    timestamped = [
        segment for segment in segments
        if isinstance(segment.get("start"), (int, float))
        and isinstance(segment.get("end"), (int, float))
    ]
    if not timestamped:
        return True, None
    start = min(float(segment["start"]) for segment in timestamped)
    end = max(float(segment["end"]) for segment in timestamped)
    duration = end - start
    if duration <= 0:
        return False, None
    rate = len(_text_tokens(_render_text(segments))) / duration
    return 0.35 <= rate <= 5.5, round(rate, 4)



def _candidate_decision(original, candidate, context_terms):
    original_text = _render_text(original)
    candidate_text = _render_text(candidate)
    original_score = _quality_score(original)
    candidate_score = _quality_score(candidate)
    if not candidate_text:
        return False, "empty_candidate", original_score, candidate_score, 0.0
    if candidate_text == original_text:
        return False, "unchanged", original_score, candidate_score, 1.0
    similarity = SequenceMatcher(
        None,
        _text_tokens(original_text),
        _text_tokens(candidate_text),
        autojunk=False,
    ).ratio()
    _annotated, assessments = annotate_segments(candidate, context_terms)
    candidate_unresolved = any(item.needs_redecode for item in assessments)
    original_unresolved = any(
        assess_segment(segment, index, context_terms).needs_redecode
        for index, segment in enumerate(original)
    )
    original_prompt_echo = _looks_like_prompt_echo(
        original_text, context_terms)
    candidate_prompt_echo = _looks_like_prompt_echo(
        candidate_text, context_terms)
    plausible_rate, _word_rate = _plausible_word_rate(candidate)
    improved = candidate_score >= original_score + 0.08
    if (
            original_prompt_echo
            and not candidate_prompt_echo
            and original_unresolved
            and not candidate_unresolved
            and plausible_rate
            and improved):
        return (
            True,
            "prompt_echo_recovered",
            original_score,
            candidate_score,
            similarity,
        )
    if original_text:
        length_ratio = len(candidate_text) / len(original_text)
        if length_ratio < 0.45 or length_ratio > 2.2:
            return (
                False,
                "implausible_length",
                original_score,
                candidate_score,
                similarity,
            )
        if similarity < 0.35:
            return (
                False,
                "divergent_candidate",
                original_score,
                candidate_score,
                similarity,
            )

    resolved = (
        original_unresolved
        and not candidate_unresolved
        and candidate_score >= original_score - 0.05
    )
    if improved or resolved:
        return (
            True,
            "quality_improved" if improved else "risk_resolved",
            original_score,
            candidate_score,
            similarity,
        )
    return (
        False,
        "quality_not_improved",
        original_score,
        candidate_score,
        similarity,
    )


def refine_segments(
        segments,
        decode_range: RangeDecoder,
        context: AsrContext,
        max_ranges=8,
        max_duration_seconds=45.0,
):
    """Refine difficult timestamped segments while preserving an audit trail."""
    annotated, assessments = annotate_segments(segments, context.terms)
    ranges, truncated = build_refinement_ranges(
        annotated,
        assessments,
        max_duration_seconds=max_duration_seconds,
        max_ranges=max_ranges,
    )
    attempts = []
    replacements = {}

    for target in ranges:
        original = annotated[target.start_index:target.end_index + 1]
        original_text = _render_text(original)
        attempt = {
            "start": target.start,
            "end": target.end,
            "segment_indexes": [
                target.start_index,
                target.end_index,
            ],
            "risk": target.risk,
            "reasons": list(target.reasons),
            "original_text": original_text,
            "original_sha256": _text_sha256(original_text),
        }
        try:
            candidate = [
                dict(segment)
                for segment in decode_range(target.start, target.end, context)
                if (segment.get("text") or "").strip()
            ]
        except Exception as exc:
            attempt.update({
                "status": "error",
                "decision": "decoder_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            attempts.append(attempt)
            continue

        candidate, _candidate_assessments = annotate_segments(
            candidate, context.terms)
        (
            accepted,
            decision,
            original_score,
            candidate_score,
            similarity,
        ) = _candidate_decision(original, candidate, context.terms)
        candidate_text = _render_text(candidate)
        attempt.update({
            "status": "accepted" if accepted else "rejected",
            "decision": decision,
            "original_quality_score": original_score,
            "candidate_quality_score": candidate_score,
            "candidate_similarity": round(similarity, 4),
            "candidate_text": candidate_text,
            "candidate_sha256": _text_sha256(candidate_text),
        })
        attempts.append(attempt)
        if not accepted:
            continue

        for segment in candidate:
            segment["refinement"] = {
                "kind": "adaptive_redecode",
                "range_start": target.start,
                "range_end": target.end,
                "original_sha256": attempt["original_sha256"],
                "decision": decision,
            }
        replacements[target.start_index] = (target.end_index, candidate)

    result = []
    cursor = 0
    for target in ranges:
        if cursor < target.start_index:
            result.extend(annotated[cursor:target.start_index])
        replacement = replacements.get(target.start_index)
        if replacement:
            end_index, candidate = replacement
            result.extend(candidate)
            cursor = end_index + 1
        else:
            original = annotated[target.start_index:target.end_index + 1]
            status = next(
                (
                    attempt["status"]
                    for attempt in attempts
                    if attempt["segment_indexes"][0] == target.start_index
                ),
                "not_attempted",
            )
            for segment in original:
                segment["refinement_status"] = status
            result.extend(original)
            cursor = target.end_index + 1
    result.extend(annotated[cursor:])

    return {
        "segments": result,
        "meta": {
            "enabled": True,
            "selected_segments": sum(
                assessment.needs_redecode for assessment in assessments),
            "candidate_ranges": len(ranges),
            "truncated_ranges": truncated,
            "accepted_ranges": sum(
                attempt.get("status") == "accepted" for attempt in attempts),
            "rejected_ranges": sum(
                attempt.get("status") == "rejected" for attempt in attempts),
            "failed_ranges": sum(
                attempt.get("status") == "error" for attempt in attempts),
            "remaining_segments": sum(
                bool(segment.get("needs_redecode")) for segment in result),
            "attempts": attempts,
        },
    }

"""Deterministic risk sampling, not accuracy or audio verification."""
import hashlib
import re
from collections import Counter
from pathlib import Path

from scripts import config
from scripts.atomic_io import atomic_write_json

ARTIFACT_NAME = "transcript_spot_check.json"


def spot_check_transcript(folder, raw, source_kind="web_transcript"):
    folder = Path(folder)
    text = (folder / "原始转录.txt").read_text(encoding="utf-8")
    segments = raw.get("segments", [])
    starts = sorted({0, max(0, len(text) // 2 - 250), max(0, len(text) - 500)})
    windows = [{"start": start, "end": min(len(text), start + 500)}
               for start in starts]
    findings = []
    patterns = {
        "amount": r"[$¥￥]|\b(?:USD|CNY|EUR)\b|\b\d{5,}\b|\d+(?:\.\d+)?\s*[万亿]",
        "percent": r"\d+(?:\.\d+)?\s*[%％]",
        "year": r"\b(?:19|20)\d{2}\b",
        "ratio": r"\d+\s*[:/]\s*\d+",
        "speaker": r"(?m)^\s*(?:Speaker\s*\d+|说话人\s*\d+|[A-Z][\w ]{0,30})[:：]",
        "placeholder": (r"lorem\s+ipsum|transcript\s+(?:is\s+)?unavailable|"
                        r"\[(?:inaudible|unintelligible|music)\]|"
                        r"暂无转录|转录不可用|�{3,}|\?{5,}"),
    }
    seen = set()
    for window in windows:
        sample = text[window["start"]:window["end"]]
        for kind, pattern in patterns.items():
            for match in re.finditer(pattern, sample, re.IGNORECASE):
                offset = window["start"] + match.start()
                if (kind, offset) not in seen:
                    seen.add((kind, offset))
                    findings.append({"kind": kind, "offset": offset, "text": match.group()})
    reasons = []
    meaningful_chars = len(re.findall(r"[^\W_]", text, re.UNICODE))
    if meaningful_chars < 80:
        reasons.append("empty_or_near_empty")
        findings.append({"kind": "empty", "meaningful_chars": meaningful_chars})
    if any(item["kind"] == "placeholder" for item in findings):
        reasons.append("sample_contains_placeholder")
    meta = raw.get("meta", {}) or {}
    duration = meta.get("audio_duration_seconds", meta.get("duration_seconds", 0))
    timestamped = [s for s in segments if s.get("start") is not None and s.get("end") is not None]
    if isinstance(duration, (int, float)) and duration >= 300 and timestamped:
        if len(timestamped) < duration / 120:
            reasons.append("too_few_timestamped_segments_for_duration")
    if not config.WEB_TRANSCRIPT_SPOT_CHECK:
        reasons.append("spot_check_disabled_requires_full_correction")
    if config.WEB_TRANSCRIPT_REQUIRE_FULL_CORRECTION:
        reasons.append("full_correction_requested")
    result = {
        "schema_version": 1,
        "verification_scope": "sample_only",
        "full_correction": False,
        "human_audio": False,
        "source_kind": source_kind,
        "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "passed": not reasons,
        "escalate_to_full_correction": bool(reasons),
        "escalation_reasons": reasons,
        "sample": {"opening_chars": min(500, len(text)), "windows": windows,
                   "mid_windows": windows[1:-1],
                   "boundary_segment_ids": [s.get("id", i) for i, s in enumerate(segments)
                                            if i in {0, len(segments) - 1}]},
        "findings": findings,
        "metrics": {"total_chars": len(text), "meaningful_chars": meaningful_chars,
                    "segment_count": len(segments), "finding_counts": dict(Counter(
                        item["kind"] for item in findings))},
    }
    atomic_write_json(folder / ARTIFACT_NAME, result)
    return result

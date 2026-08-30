"""Deterministic audio/transcript interval completeness metrics."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


COMPLETENESS_SCHEMA_VERSION = 1
COMPLETENESS_CONTRACT_VERSION = 1
COMPLETENESS_MODES = frozenset({"report_only", "enforce"})
DEFAULT_POLICY = {
    "min_speech_coverage": 0.98,
    "max_uncovered_speech_seconds": 3.0,
    "max_edge_gap_seconds": 2.0,
    "boundary_tolerance_seconds": 0.5,
}


class CompletenessUnavailable(RuntimeError):
    pass


def parse_contract_version(value, *, field="contract_version"):
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是非负整数")
    if value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def normalize_completeness_mode(value):
    mode = str(value or "report_only").strip().lower()
    if mode not in COMPLETENESS_MODES:
        raise ValueError(
            f"completeness mode 必须是 {sorted(COMPLETENESS_MODES)}")
    return mode


def completeness_contract_required(raw):
    meta = raw.get("meta", {}) or {}
    return parse_contract_version(
        meta.get("completeness_contract_version"),
        field="completeness_contract_version",
    ) >= COMPLETENESS_CONTRACT_VERSION


def completeness_enforcement_mode(raw):
    return normalize_completeness_mode(
        (raw.get("meta", {}) or {}).get("completeness_mode", "report_only"))


@dataclass(frozen=True)
class CompletenessPolicy:
    min_speech_coverage: float = 0.98
    max_uncovered_speech_seconds: float = 3.0
    max_edge_gap_seconds: float = 2.0
    boundary_tolerance_seconds: float = 0.5

    def to_dict(self):
        return {
            "min_speech_coverage": self.min_speech_coverage,
            "max_uncovered_speech_seconds": self.max_uncovered_speech_seconds,
            "max_edge_gap_seconds": self.max_edge_gap_seconds,
            "boundary_tolerance_seconds": self.boundary_tolerance_seconds,
        }


VadDetector = Callable[[str], Sequence[tuple[float, float]]]


def audio_duration_seconds(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise CompletenessUnavailable("ffprobe 无法读取音频时长")
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise CompletenessUnavailable("音频时长无效")
    return duration


def _decode_pcm_chunk(path, start, duration, sampling_rate=16000):
    import numpy as np

    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
            "-i", str(path), "-t", f"{duration:.3f}", "-ac", "1",
            "-ar", str(sampling_rate), "-f", "f32le", "pipe:1",
        ],
        capture_output=True,
        timeout=max(60, min(1800, round(duration * 2 + 30))),
    )
    if result.returncode != 0:
        raise CompletenessUnavailable(
            "ffmpeg VAD 分块解码失败: "
            + result.stderr.decode("utf-8", errors="replace")[-1000:])
    return np.frombuffer(result.stdout, dtype=np.float32)


def _silero_intervals(audio, options, sampling_rate=16000):
    from faster_whisper.vad import get_speech_timestamps

    return get_speech_timestamps(
        audio, vad_options=options, sampling_rate=sampling_rate)


def faster_whisper_vad(
        path, *, chunk_seconds=900.0, overlap_seconds=1.0,
        duration=None, decode_chunk=None, vad_runner=None):
    """Run a bounded-memory sensitive Silero VAD pass over audio chunks."""
    try:
        from faster_whisper.vad import VadOptions
    except (ImportError, AttributeError) as exc:
        raise CompletenessUnavailable(
            "当前 faster-whisper 未提供可用的 Silero VAD adapter") from exc
    duration = float(duration or audio_duration_seconds(path))
    chunk_seconds = float(chunk_seconds)
    overlap_seconds = float(overlap_seconds)
    if (
            chunk_seconds <= 0
            or overlap_seconds < 0
            or overlap_seconds >= chunk_seconds):
        raise ValueError("VAD chunk/overlap 参数无效")
    options = VadOptions(
        threshold=0.25,
        min_speech_duration_ms=100,
        min_silence_duration_ms=250,
        speech_pad_ms=200,
    )
    decode = decode_chunk or _decode_pcm_chunk
    run_vad = vad_runner or _silero_intervals
    sampling_rate = 16000
    step = chunk_seconds - overlap_seconds
    intervals = []
    offset = 0.0
    while offset < duration:
        active_duration = min(chunk_seconds, duration - offset)
        audio = decode(str(path), offset, active_duration, sampling_rate)
        for item in run_vad(audio, options, sampling_rate):
            start = offset + float(item["start"]) / sampling_rate
            end = offset + float(item["end"]) / sampling_rate
            if end > start:
                intervals.append((start, min(end, duration)))
        if offset + active_duration >= duration:
            break
        offset += step
    return normalize_intervals(intervals, duration)


def normalize_intervals(intervals: Iterable[tuple[float, float]], duration=None):
    cleaned = []
    for start, end in intervals:
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if duration is not None:
            start, end = max(0.0, start), min(float(duration), end)
        if end > start >= 0:
            cleaned.append((start, end))
    cleaned.sort()
    merged = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def interval_seconds(intervals):
    return sum(end - start for start, end in intervals)


def subtract_intervals(base, covered):
    remaining = []
    covered = normalize_intervals(covered)
    for start, end in normalize_intervals(base):
        cursor = start
        for cover_start, cover_end in covered:
            if cover_end <= cursor:
                continue
            if cover_start >= end:
                break
            if cover_start > cursor:
                remaining.append((cursor, min(cover_start, end)))
            cursor = max(cursor, cover_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
    return normalize_intervals(remaining)


def segment_intervals(segments):
    return [
        (segment.get("start"), segment.get("end"))
        for segment in segments
        if (segment.get("text") or "").strip()
        and isinstance(segment.get("start"), (int, float))
        and isinstance(segment.get("end"), (int, float))
    ]


def segment_timeline_sha256(segments):
    payload = [
        {
            "id": segment.get("id"),
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text": segment.get("text", ""),
        }
        for segment in segments
        if isinstance(segment, dict) and (segment.get("text") or "").strip()
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def timeline_errors(segments, duration):
    errors = []
    previous_start = -1.0
    for index, segment in enumerate(segments):
        if not (segment.get("text") or "").strip():
            continue
        start, end = segment.get("start"), segment.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"segment[{index}] 缺少时间范围")
            continue
        if start < 0 or end <= start or end > duration + 0.5:
            errors.append(f"segment[{index}] 时间范围无效: {start}-{end}")
        if start < previous_start:
            errors.append(f"segment[{index}] 时间戳非单调")
        previous_start = start
    return errors


def calculate_completeness(
        duration, speech_intervals, segments, *, policy=None,
        detector="injected", mode="report_only"):
    policy = policy or CompletenessPolicy()
    mode = normalize_completeness_mode(mode)
    speech = normalize_intervals(speech_intervals, duration)
    transcript = normalize_intervals(segment_intervals(segments), duration)
    tolerance = policy.boundary_tolerance_seconds
    expanded = normalize_intervals(
        ((max(0.0, start - tolerance), min(duration, end + tolerance))
         for start, end in transcript),
        duration,
    )
    uncovered = subtract_intervals(speech, expanded)
    speech_seconds = interval_seconds(speech)
    uncovered_seconds = interval_seconds(uncovered)
    covered_seconds = max(0.0, speech_seconds - uncovered_seconds)
    coverage = covered_seconds / speech_seconds if speech_seconds else 0.0
    first_speech = speech[0][0] if speech else None
    last_speech = speech[-1][1] if speech else None
    first_segment = transcript[0][0] if transcript else None
    last_segment = transcript[-1][1] if transcript else None
    first_gap = (
        max(0.0, first_segment - first_speech)
        if first_speech is not None and first_segment is not None else None
    )
    last_gap = (
        max(0.0, last_speech - last_segment)
        if last_speech is not None and last_segment is not None else None
    )
    timeline = timeline_errors(segments, duration)
    max_uncovered = max((end - start for start, end in uncovered), default=0.0)
    if not speech:
        status = "detector_mismatch" if transcript else "no_speech"
    else:
        status = "measured"
    passed = (
        bool(speech)
        and not timeline
        and bool(transcript)
        and coverage >= policy.min_speech_coverage
        and max_uncovered <= policy.max_uncovered_speech_seconds
        and (first_gap is None or first_gap <= policy.max_edge_gap_seconds)
        and (last_gap is None or last_gap <= policy.max_edge_gap_seconds)
    )
    return {
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "status": "passed" if passed else status if not speech else "failed",
        "enforcement_mode": mode,
        "detector": detector,
        "segment_timeline_sha256": segment_timeline_sha256(segments),
        "audio_duration_seconds": round(duration, 3),
        "speech_seconds": round(speech_seconds, 3),
        "covered_speech_seconds": round(covered_seconds, 3),
        "speech_coverage": round(coverage, 4),
        "first_speech_start": first_speech,
        "first_segment_start": first_segment,
        "first_speech_gap_seconds": round(first_gap, 3) if first_gap is not None else None,
        "last_speech_end": last_speech,
        "last_segment_end": last_segment,
        "last_speech_gap_seconds": round(last_gap, 3) if last_gap is not None else None,
        "uncovered_intervals": [
            {"start": round(start, 3), "end": round(end, 3),
             "duration": round(end - start, 3)}
            for start, end in uncovered
        ],
        "max_uncovered_speech_seconds": round(max_uncovered, 3),
        "timeline_valid": not timeline,
        "timeline_errors": timeline,
        "policy": {**policy.to_dict(), "enforcement_mode": mode},
        "passed": passed,
    }


def validate_completeness_result(raw, result=None):
    """Validate a stored result instead of trusting its ``passed`` flag."""
    errors = []
    meta = (raw or {}).get("meta", {}) or {}
    result = result if result is not None else meta.get("completeness")
    if not isinstance(result, dict):
        return ["语音完整性报告必须是对象"]
    if result.get("schema_version") != COMPLETENESS_SCHEMA_VERSION:
        errors.append("语音完整性报告 schema 不受支持")
    try:
        expected_mode = completeness_enforcement_mode(raw or {})
    except ValueError as exc:
        expected_mode = None
        errors.append(str(exc))
    if result.get("enforcement_mode") != expected_mode:
        errors.append("语音完整性报告 rollout mode 与 revision 不一致")
    policy = result.get("policy")
    if not isinstance(policy, dict):
        errors.append("语音完整性报告缺少 policy")
        policy = {}
    elif policy.get("enforcement_mode") != expected_mode:
        errors.append("语音完整性 policy rollout mode 与 revision 不一致")
    if result.get("segment_timeline_sha256") != segment_timeline_sha256(
            (raw or {}).get("segments", [])):
        errors.append("语音完整性报告未绑定当前 segment 时间线")

    status = result.get("status")
    passed = result.get("passed")
    if not isinstance(passed, bool):
        errors.append("语音完整性 passed 必须是布尔值")
    if status not in {
            "passed", "failed", "detector_mismatch", "no_speech",
            "unavailable"}:
        errors.append("语音完整性 status 无效")
    if passed is True and status != "passed":
        errors.append("语音完整性 passed/status 不一致")
    if passed is not True:
        return errors

    def finite_number(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    numeric_fields = (
        "audio_duration_seconds", "speech_seconds", "covered_speech_seconds",
        "speech_coverage", "max_uncovered_speech_seconds",
    )
    for field in numeric_fields:
        if not finite_number(result.get(field)):
            errors.append(f"语音完整性缺少有效数值字段: {field}")
    duration = result.get("audio_duration_seconds")
    speech = result.get("speech_seconds")
    covered = result.get("covered_speech_seconds")
    coverage = result.get("speech_coverage")
    max_gap = result.get("max_uncovered_speech_seconds")
    if finite_number(duration) and duration <= 0:
        errors.append("语音完整性音频时长无效")
    if finite_number(speech) and speech <= 0:
        errors.append("语音完整性通过结果必须检测到语音")
    if all(finite_number(value) for value in (speech, duration)) \
            and speech > duration + 0.002:
        errors.append("语音完整性检测语音时长超过音频时长")
    if all(finite_number(value) for value in (speech, covered)):
        if covered < 0 or covered > speech:
            errors.append("语音完整性覆盖时长无效")
        elif speech > 0 and finite_number(coverage):
            if abs(covered / speech - coverage) > 0.002:
                errors.append("语音完整性 coverage 内部不一致")

    current_segments = (raw or {}).get("segments", [])
    current_timeline = normalize_intervals(segment_intervals(current_segments))
    current_first = current_timeline[0][0] if current_timeline else None
    current_last = current_timeline[-1][1] if current_timeline else None
    if finite_number(duration):
        current_errors = timeline_errors(current_segments, duration)
        if current_errors:
            errors.append("语音完整性报告的音频时长与当前时间线不一致")
        if current_last is not None and current_last > duration + 0.002:
            errors.append("语音完整性当前 segment 超出报告音频时长")
    for field, expected in (
            ("first_segment_start", current_first),
            ("last_segment_end", current_last)):
        value = result.get(field)
        if expected is None or not finite_number(value) or abs(value - expected) > 0.002:
            errors.append(f"语音完整性 {field} 与当前 segment 不一致")

    first_speech = result.get("first_speech_start")
    last_speech = result.get("last_speech_end")
    first_edge = result.get("first_speech_gap_seconds")
    last_edge = result.get("last_speech_gap_seconds")
    if all(finite_number(value) for value in (
            duration, first_speech, last_speech, first_edge, last_edge)):
        if not (0 <= first_speech <= last_speech <= duration):
            errors.append("语音完整性语音边界无效")
        if current_first is not None and abs(
                max(0.0, current_first - first_speech) - first_edge) > 0.002:
            errors.append("语音完整性首段边界内部不一致")
        if current_last is not None and abs(
                max(0.0, last_speech - current_last) - last_edge) > 0.002:
            errors.append("语音完整性末段边界内部不一致")
    else:
        errors.append("语音完整性通过结果缺少有效首尾边界")

    if result.get("timeline_valid") is not True:
        errors.append("语音完整性通过结果的时间线无效")
    if result.get("timeline_errors") not in ([], None):
        errors.append("语音完整性通过结果仍含时间线错误")
    if not str(result.get("detector") or "").strip():
        errors.append("语音完整性报告缺少 detector")
    uncovered = result.get("uncovered_intervals")
    if not isinstance(uncovered, list):
        errors.append("语音完整性 uncovered_intervals 必须是数组")
        uncovered = []
    uncovered_total = 0.0
    uncovered_max = 0.0
    for index, item in enumerate(uncovered):
        if not isinstance(item, dict) or not all(
                finite_number(item.get(field))
                for field in ("start", "end", "duration")):
            errors.append(f"语音完整性 uncovered_intervals[{index}] 无效")
            continue
        start, end, item_duration = (
            item["start"], item["end"], item["duration"])
        if start < 0 or end <= start or (
                finite_number(duration) and end > duration + 0.002):
            errors.append(f"语音完整性 uncovered_intervals[{index}] 越界")
            continue
        if abs((end - start) - item_duration) > 0.002:
            errors.append(f"语音完整性 uncovered_intervals[{index}] 时长不一致")
        uncovered_total += end - start
        uncovered_max = max(uncovered_max, end - start)
    interval_tolerance = max(0.01, len(uncovered) * 0.002)
    if all(finite_number(value) for value in (speech, covered)) and abs(
            (speech - covered) - uncovered_total) > interval_tolerance:
        errors.append("语音完整性未覆盖时长内部不一致")
    if finite_number(max_gap) and abs(
            max_gap - uncovered_max) > interval_tolerance:
        errors.append("语音完整性最大未覆盖区间内部不一致")

    expected_policy = CompletenessPolicy().to_dict()
    for field, expected in expected_policy.items():
        value = policy.get(field)
        if not finite_number(value) or abs(value - expected) > 1e-9:
            errors.append(f"语音完整性 policy 与当前配置不一致: {field}")

    threshold_fields = {
        "min_speech_coverage": (coverage, lambda value, limit: value >= limit),
        "max_uncovered_speech_seconds": (
            max_gap, lambda value, limit: value <= limit),
    }
    for field, (value, predicate) in threshold_fields.items():
        limit = expected_policy[field]
        if finite_number(value) and not predicate(value, limit):
            errors.append(f"语音完整性通过结果不满足阈值: {field}")
    edge_limit = expected_policy["max_edge_gap_seconds"]
    for field in ("first_speech_gap_seconds", "last_speech_gap_seconds"):
        value = result.get(field)
        if finite_number(value) and value > edge_limit:
            errors.append(f"语音完整性通过结果不满足阈值: {field}")
    return errors


def analyze_audio_completeness(
        audio_path, segments, *, detector: VadDetector | None = None,
        duration=None, policy=None, mode="report_only"):
    mode = normalize_completeness_mode(mode)
    active_detector = detector or faster_whisper_vad
    try:
        duration = float(duration or audio_duration_seconds(audio_path))
        speech = active_detector(str(audio_path))
        return calculate_completeness(
            duration, speech, segments, policy=policy,
            detector=getattr(active_detector, "__name__", "vad"),
            mode=mode,
        )
    except Exception as exc:
        return {
            "schema_version": COMPLETENESS_SCHEMA_VERSION,
            "status": "unavailable",
            "enforcement_mode": mode,
            "detector": getattr(active_detector, "__name__", "vad"),
            "segment_timeline_sha256": segment_timeline_sha256(segments),
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "policy": {
                **(policy or CompletenessPolicy()).to_dict(),
                "enforcement_mode": mode,
            },
        }

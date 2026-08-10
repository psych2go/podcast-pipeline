"""Forced-alignment seam with Whisper timestamp and WhisperX adapters."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AlignmentRequest:
    audio_path: Path
    segments: tuple[dict, ...]
    language: str
    device: str
    model_name: str | None


AlignmentRunner = Callable[[AlignmentRequest], dict]


def _tokens(text):
    return re.findall(
        r"[^\W_]+(?:['’-][^\W_]+)*",
        (text or "").lower(),
        flags=re.UNICODE,
    )


def _render_text(segments):
    return " ".join(
        (segment.get("text") or "").strip()
        for segment in segments
        if (segment.get("text") or "").strip()
    ).strip()


def _alignment_python():
    configured = os.environ.get("ALIGNMENT_PYTHON", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / ".venv-alignment" / "bin" / "python"


def alignment_available():
    python = _alignment_python()
    adapter = Path(__file__).resolve().parent / "whisperx_align_adapter.py"
    return python.is_file() and adapter.is_file()


def _subprocess_runner(request):
    python = _alignment_python()
    if not python.is_file():
        raise RuntimeError(
            f"WhisperX alignment 环境不存在: {python}；"
            "请运行 scripts/setup_alignment_env.py"
        )
    adapter = Path(__file__).resolve().parent / "whisperx_align_adapter.py"
    timeout = int(os.environ.get("ALIGNMENT_TIMEOUT", "3600"))
    with tempfile.TemporaryDirectory(prefix="asr-align-") as temp_dir:
        input_path = Path(temp_dir) / "input.json"
        output_path = Path(temp_dir) / "output.json"
        input_path.write_text(
            json.dumps(
                {"segments": list(request.segments)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        command = [
            str(python),
            str(adapter),
            "--audio", str(request.audio_path),
            "--input", str(input_path),
            "--output", str(output_path),
            "--language", request.language,
            "--device", request.device,
        ]
        if request.model_name:
            command.extend(["--model", request.model_name])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "NLTK_DATA": os.environ.get(
                    "NLTK_DATA",
                    str(python.parents[1] / "nltk_data"),
                ),
            },
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"WhisperX alignment 失败: {message[-2000:]}")
        if not output_path.exists():
            raise RuntimeError("WhisperX alignment 未生成输出")
        return json.loads(output_path.read_text(encoding="utf-8"))


def _normalize_word(word):
    item = {
        "word": word.get("word", ""),
        "start": word.get("start"),
        "end": word.get("end"),
    }
    probability = word.get("probability", word.get("score"))
    if probability is not None:
        item["probability"] = probability
    return item


def _apply_alignment(original, payload):
    aligned_segments = payload.get("segments", [])
    if _tokens(_render_text(original)) != _tokens(_render_text(aligned_segments)):
        raise RuntimeError("强制对齐改变了转录词序，拒绝替换时间戳")

    by_source = {}
    for aligned in aligned_segments:
        source_index = aligned.get("source_index")
        if not isinstance(source_index, int):
            continue
        by_source.setdefault(source_index, []).append(aligned)

    result = []
    timestamped_words = 0
    total_words = 0
    aligned_segment_count = 0
    for index, source in enumerate(original):
        segment = dict(source)
        matched = by_source.get(index, [])
        words = [
            _normalize_word(word)
            for aligned in matched
            for word in (aligned.get("words") or [])
            if (word.get("word") or "").strip()
        ]
        total_words += len(_tokens(segment.get("text", "")))
        timestamped_words += sum(
            word.get("start") is not None and word.get("end") is not None
            for word in words
        )
        if words:
            aligned_segment_count += 1
            segment["words"] = words
            starts = [
                word["start"] for word in words
                if word.get("start") is not None
            ]
            ends = [
                word["end"] for word in words
                if word.get("end") is not None
            ]
            if starts and ends:
                segment["alignment_original_start"] = segment.get("start")
                segment["alignment_original_end"] = segment.get("end")
                segment["start"] = min(starts)
                segment["end"] = max(ends)
            segment["alignment"] = {
                "kind": "whisperx_forced",
                "status": "aligned",
                "model": payload.get("meta", {}).get("model"),
                "device": payload.get("meta", {}).get("device"),
            }
        else:
            segment["alignment"] = {
                "kind": "whisperx_forced",
                "status": "unaligned",
                "model": payload.get("meta", {}).get("model"),
                "device": payload.get("meta", {}).get("device"),
            }
            segment["needs_review"] = True
        result.append(segment)

    coverage = (
        timestamped_words / total_words
        if total_words else 0.0
    )
    return {
        "segments": result,
        "meta": {
            "enabled": True,
            "adapter": "whisperx",
            "status": "complete" if coverage >= 0.9 else "partial",
            "model": payload.get("meta", {}).get("model"),
            "device": payload.get("meta", {}).get("device"),
            "sentence_splitter": payload.get(
                "meta", {}).get("sentence_splitter"),
            "elapsed_seconds": payload.get("meta", {}).get("elapsed_seconds"),
            "aligned_segments": aligned_segment_count,
            "segment_count": len(result),
            "timestamped_words": timestamped_words,
            "reference_words": total_words,
            "word_timestamp_coverage": round(coverage, 4),
        },
    }


def align_segments(
        audio_path,
        segments,
        *,
        language="en",
        mode="auto",
        device="cpu",
        model_name=None,
        runner: AlignmentRunner | None = None,
):
    """Align final text while preserving content and source segment metadata."""
    original = [dict(segment) for segment in segments]
    if mode not in {"auto", "whisperx", "none"}:
        raise ValueError(f"未知 alignment mode: {mode}")
    if mode == "none":
        return {
            "segments": original,
            "meta": {
                "enabled": False,
                "adapter": "whisper_timestamps",
                "status": "disabled",
            },
        }
    if mode == "auto" and runner is None and not alignment_available():
        return {
            "segments": original,
            "meta": {
                "enabled": False,
                "adapter": "whisper_timestamps",
                "status": "unavailable",
                "warning": "alignment_environment_missing",
            },
        }

    request = AlignmentRequest(
        audio_path=Path(audio_path),
        segments=tuple(original),
        language=language or "en",
        device=device,
        model_name=model_name,
    )
    active_runner = runner or _subprocess_runner
    try:
        payload = active_runner(request)
        return _apply_alignment(original, payload)
    except Exception as exc:
        if mode == "whisperx":
            raise
        return {
            "segments": original,
            "meta": {
                "enabled": False,
                "adapter": "whisper_timestamps",
                "status": "failed",
                "warning": "alignment_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        }

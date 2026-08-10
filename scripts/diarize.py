"""
说话人分离 + ASR 时间戳对齐。

输入是统一 ASR 产出的带 segment/word timestamp 的结构，输出仍保留
speaker label，同时尽量在说话人切换处切开文本，避免整段按中点误归属。
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

from config import HF_TOKEN, require_hf_token

_PIPELINE = None


def _load_pipeline():
    """加载 pyannote 管线；允许通过 DIARIZATION_MODEL 覆盖模型名。"""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE

    token = HF_TOKEN or require_hf_token()
    import torch
    torch.set_num_threads(min(6, os.cpu_count() or 6))
    model_name = os.environ.get(
        "DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1")
    from pyannote.audio import Pipeline

    print(f"[Diarize] 加载 {model_name}...", flush=True)
    started = time.time()
    pipe = Pipeline.from_pretrained(model_name, token=token)
    pipe.to(torch.device("cpu"))
    print(f"[Diarize] 加载完成 {time.time() - started:.1f}s", flush=True)
    _PIPELINE = pipe
    return pipe


def _read_audio_mono(path):
    """读音频为 16kHz 单声道 float32。"""
    import numpy as np
    import soundfile as sf
    try:
        audio, sr = sf.read(path, dtype="float32")
    except Exception:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", tmp_path],
                capture_output=True, timeout=600, check=True)
            audio, sr = sf.read(tmp_path, dtype="float32")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        import torch
        import torchaudio
        audio = torchaudio.functional.resample(
            torch.from_numpy(audio), sr, 16000).numpy()
        sr = 16000
    return audio.astype("float32"), sr


def diarize(audio_path, min_speakers=None, max_speakers=None):
    """跑 pyannote，返回 [(start, end, speaker_label), ...]。"""
    pipe = _load_pipeline()
    audio, sr = _read_audio_mono(audio_path)
    import torch

    kwargs = {}
    if min_speakers:
        kwargs["min_speakers"] = min_speakers
    if max_speakers:
        kwargs["max_speakers"] = max_speakers

    print(f"[Diarize] 分析 {len(audio) / sr / 60:.1f} 分钟音频...", flush=True)
    started = time.time()
    waveform = torch.from_numpy(audio).unsqueeze(0)
    output = pipe({"waveform": waveform, "sample_rate": sr}, **kwargs)

    # pyannote 3.x/4.x 的兼容读取。
    turns = []
    if hasattr(output, "serialize"):
        serialized = output.serialize()
        for item in serialized.get("diarization", []):
            turns.append((float(item["start"]), float(item["end"]), item["speaker"]))
    elif hasattr(output, "itertracks"):
        for segment, _, label in output.itertracks(yield_label=True):
            turns.append((float(segment.start), float(segment.end), label))
    turns.sort(key=lambda item: (item[0], item[1]))
    print(
        f"[Diarize] 完成 {time.time() - started:.0f}s，"
        f"{len(set(t[2] for t in turns))} 个说话人，{len(turns)} 段",
        flush=True,
    )
    return turns


def _overlap(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _speaker_for_interval(start, end, turns):
    if not turns:
        return "SPEAKER_00"
    best = None
    best_overlap = 0.0
    best_distance = float("inf")
    midpoint = (start + end) / 2
    for turn_start, turn_end, label in turns:
        overlap = _overlap(start, end, turn_start, turn_end)
        distance = 0.0 if overlap else min(abs(midpoint - turn_start), abs(midpoint - turn_end))
        if overlap > best_overlap or (overlap == best_overlap and distance < best_distance):
            best = label
            best_overlap = overlap
            best_distance = distance
    return best or turns[0][2]


def _split_cleaned_text(text, group_sizes):
    """按原始词组规模把清理后的文本分配到 speaker 片段。

    ASR 清理可能删除重复词或整段幻觉，不能再从原始 words 重建文本；
    因此这里以清理后的文本为唯一文字来源，只借用 words 的数量来分段。
    """
    tokens = (text or "").split()
    if not tokens:
        return [""] * len(group_sizes)
    total = sum(group_sizes) or len(group_sizes)
    result = []
    cursor = 0
    for index, size in enumerate(group_sizes):
        if index == len(group_sizes) - 1:
            end = len(tokens)
        else:
            end = round(len(tokens) * (sum(group_sizes[:index + 1]) / total))
            end = max(cursor + (1 if cursor < len(tokens) else 0), end)
            end = min(end, len(tokens))
        result.append(" ".join(tokens[cursor:end]))
        cursor = end
    return result


def _word_tokens(text):
    return re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", (text or "").lower())


def _cleaning_changed_word_sequence(segment):
    """判断清理是否改变了词序/词数量；改变时不能伪造词级 speaker 归属。"""
    raw_text = " ".join((word.get("word") or "").strip()
                         for word in (segment.get("words") or []))
    return _word_tokens(raw_text) != _word_tokens(segment.get("text", ""))


def _copy_segment_metadata(segment, start, end, text, words, speaker):
    """复制 segment 元数据，避免 diarization 丢掉置信度和原始词信息。"""
    item = dict(segment)
    item.update({
        "start": float(start),
        "end": float(end),
        "text": text,
        "speaker": speaker,
        "words": words,
    })
    return item


def _split_segment_by_words(segment, turns):
    """按词时间戳切分 speaker，同时始终使用清理后的 segment 文本。"""
    words = segment.get("words") or []
    if not words:
        item = dict(segment)
        item["speaker"] = _speaker_for_interval(segment["start"], segment["end"], turns)
        return [item]

    # 清理删除/修改了词时，不能按原始词数量把剩余文本硬分配给 speaker；
    # 否则广告或幻觉被删掉后，剩下的核心观点可能被归给错误的人。
    if _cleaning_changed_word_sequence(segment):
        item = dict(segment)
        item["speaker"] = None
        item["speaker_alignment"] = "unresolved"
        item["needs_review"] = True
        return [item]

    groups = []
    current = None
    for word in words:
        raw_text = (word.get("word") or "").strip()
        if not raw_text:
            continue
        start = word.get("start")
        end = word.get("end")
        if start is None or end is None:
            start, end = segment["start"], segment["end"]
        start, end = float(start), float(end)
        speaker = _speaker_for_interval(start, end, turns)
        if current is None or current["speaker"] != speaker:
            if current:
                groups.append(current)
            current = {
                "start": start,
                "end": end,
                "speaker": speaker,
                "words": [],
            }
        else:
            current["end"] = end
        current["words"].append(word)
    if current:
        groups.append(current)
    if not groups:
        item = dict(segment)
        item["speaker"] = _speaker_for_interval(segment["start"], segment["end"], turns)
        return [item]

    texts = _split_cleaned_text(
        segment.get("text", ""), [len(group["words"]) for group in groups])
    result = []
    for group, text in zip(groups, texts):
        result.append(_copy_segment_metadata(
            segment, group["start"], group["end"], text,
            group["words"], group["speaker"]))
    return result


def merge_segments_with_speakers(segments, turns):
    """用词级重叠优先、片段重叠回退的方式对齐 speaker。"""
    merged = []
    for segment in segments:
        merged.extend(_split_segment_by_words(segment, turns))
    return merged


def render_with_speakers(merged_segments, include_timestamps=False):
    """渲染为可人工审阅文本，保留 speaker 切换和段落边界。"""
    lines = []
    previous = None
    for segment in merged_segments:
        text = segment.get("text", "").strip()
        if not text:
            continue
        speaker = segment.get("speaker", "SPEAKER_00")
        prefix = ""
        if include_timestamps:
            prefix = f"[{segment.get('start', 0):.2f}-{segment.get('end', 0):.2f}] "
        if speaker != previous:
            lines.append(f"{prefix}[{speaker}]: {text}")
        else:
            lines.append(f"{prefix}{text}")
        previous = speaker
    return "\n".join(lines).strip()


def diarize_and_merge(audio_path, segments, min_speakers=None, max_speakers=None,
                      return_segments=False):
    """跑分离、对齐 ASR 片段；默认兼容旧接口返回文本。"""
    turns = diarize(audio_path, min_speakers=min_speakers, max_speakers=max_speakers)
    merged = merge_segments_with_speakers(segments, turns)
    if return_segments:
        return merged
    return render_with_speakers(merged)


def main():
    parser = argparse.ArgumentParser(description="说话人分离（pyannote）")
    parser.add_argument("audio", help="音频文件路径")
    parser.add_argument("--segments", required=True, help="ASR 片段 JSON")
    parser.add_argument("--out", default=None, help="输出文本路径")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    args = parser.parse_args()

    with open(args.segments, encoding="utf-8") as f:
        segments = json.load(f)
    result = diarize_and_merge(
        args.audio, segments,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        return_segments=True,
    )
    text = render_with_speakers(result, include_timestamps=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[完成] → {args.out}", flush=True)
    else:
        print(text)


if __name__ == "__main__":
    main()

"""
说话人分离（pyannote 3.1）+ ASR 时间戳对齐 — v5 新增。

做什么：
  1. pyannote/speaker-diarization-3.1 把音频切成"说话人 x 时间段"网络
  2. Parakeet 转录返回带时间戳的片段（timestamped segments）
  3. 按"片段中点落在哪个说话人时间段"对齐，给每句文本标上说话人

输出格式（写进 原始转录.txt，供 Claude 写讲稿时识别说话人）：

    [SPEAKER_00]: some transcribed text here.
    [SPEAKER_01]: another speaker replies.
    [SPEAKER_00]: continues talking.

要求：HF_TOKEN 已设置且在 huggingface.co 接受了
      pyannote/speaker-diarization-3.1 和 pyannote/segmentation-3.0 的用户协议。

库接口：
    from diarize import diarize_and_merge
    text = diarize_and_merge("audio.mp3", segments)  # segments 带 start/end
CLI:
    python scripts/diarize.py audio.mp3 --segments segs.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

from config import HF_TOKEN

_PIPELINE = None


def _load_pipeline():
    """加载 pyannote speaker-diarization-3.1（单例）。需 HF_TOKEN。"""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE

    if not HF_TOKEN:
        raise RuntimeError(
            "说话人分离需要 HF_TOKEN。请在 .env 配置，并在 huggingface.co 接受 "
            "pyannote/speaker-diarization-3.1 与 pyannote/segmentation-3.0 协议。")

    import torch
    torch.set_num_threads(min(6, os.cpu_count() or 6))
    # 离线模式：已下载的模型不走网络（沙箱内 Python 网络受限）
    import os as _os
    _os.environ["HF_HUB_OFFLINE"] = "1"
    from pyannote.audio import Pipeline

    print("[Diarize] 加载 pyannote/speaker-diarization-3.1...", flush=True)
    t0 = time.time()
    pipe = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", token=HF_TOKEN)
    pipe.to(torch.device("cpu"))
    print(f"[Diarize] 加载完成 {time.time()-t0:.1f}s", flush=True)
    _PIPELINE = pipe
    return pipe


def _read_audio_mono(path):
    """读音频为 (samples, sr)，16kHz 单声道 float32 numpy。"""
    import numpy as np
    import soundfile as sf
    try:
        audio, sr = sf.read(path, dtype="float32")
    except Exception:
        import subprocess, tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", tmp.name],
            capture_output=True, timeout=600, check=True)
        audio, sr = sf.read(tmp.name, dtype="float32")
        os.unlink(tmp.name)
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
    """跑 pyannote，返回 [(start, end, speaker_label), ...] 时间轴。"""
    pipe = _load_pipeline()
    audio, sr = _read_audio_mono(audio_path)
    import torch

    kwargs = {}
    if min_speakers:
        kwargs["min_speakers"] = min_speakers
    if max_speakers:
        kwargs["max_speakers"] = max_speakers

    print(f"[Diarize] 分析 {len(audio)/sr/60:.1f} 分钟音频...", flush=True)
    t0 = time.time()
    # pyannote 接受 (channels, samples) tensor
    waveform = torch.from_numpy(audio).unsqueeze(0)
    diarization = pipe(
        {"waveform": waveform, "sample_rate": sr}, **kwargs)

    # pyannote.audio 4.x：DiarizeOutput.serialize() 返回 dict，
    # ["diarization"] 是 [{start, end, speaker}, ...]，兼容性最干净
    serialized = diarization.serialize()
    turns = [(item["start"], item["end"], item["speaker"])
             for item in serialized["diarization"]]
    print(f"[Diarize] 完成 {time.time()-t0:.0f}s，"
          f"{len(set(t[2] for t in turns))} 个说话人，"
          f"{len(turns)} 段", flush=True)
    return turns


def merge_segments_with_speakers(segments, turns):
    """把带时间戳的 ASR 片段和说话人时间段对齐。

    segments: [{"start": float, "end": float, "text": str}, ...]（秒）
    turns:    [(start, end, label), ...]（秒）

    对齐策略：取 ASR 片段的**中点**落在的说话人段。中点最稳——避免跨段边界抖动。
    输出：同 segments，加 "speaker" 字段。
    """
    labels = sorted(set(t[2] for t in turns))
    # 把相邻同说话人的 turn 合并成大区间，减少边界扫描
    merged = []
    for s, e, lab in sorted(turns):
        if merged and merged[-1][2] == lab and s <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), lab)
        else:
            merged.append((s, e, lab))

    def speaker_at(t):
        # 二分找包含 t 的段；否则取最近段
        best = None
        best_dist = float("inf")
        for s, e, lab in merged:
            if s <= t <= e:
                return lab
            dist = min(abs(t - s), abs(t - e))
            if dist < best_dist:
                best_dist = dist
                best = lab
        return best or (labels[0] if labels else "SPEAKER_00")

    out = []
    for seg in segments:
        mid = (seg["start"] + seg["end"]) / 2.0
        out.append({**seg, "speaker": speaker_at(mid)})
    return out


def render_with_speakers(merged_segments):
    """把对齐后的片段渲染成带说话人标签的文本。"""
    lines = []
    prev_spk = None
    for seg in merged_segments:
        spk = seg.get("speaker", "SPEAKER_00")
        txt = seg["text"].strip()
        if not txt:
            continue
        if spk != prev_spk:
            lines.append(f"\n[{spk}]: {txt}")
        else:
            lines.append(txt)
        prev_spk = spk
    text = " ".join(lines)
    import re
    text = re.sub(r"\s+", " ", text).strip()
    return text


def diarize_and_merge(audio_path, segments, min_speakers=None, max_speakers=None):
    """一站式：跑分离 + 对齐 ASR 片段 + 渲染带说话人文本。"""
    turns = diarize(audio_path, min_speakers=min_speakers,
                    max_speakers=max_speakers)
    merged = merge_segments_with_speakers(segments, turns)
    return render_with_speakers(merged)


def main():
    parser = argparse.ArgumentParser(description="说话人分离（pyannote 3.1）")
    parser.add_argument("audio", help="音频文件路径")
    parser.add_argument("--segments", required=True,
                        help="ASR 片段 JSON：[{start,end,text}, ...]")
    parser.add_argument("--out", default=None, help="输出文本路径")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    args = parser.parse_args()

    with open(args.segments, encoding="utf-8") as f:
        segments = json.load(f)
    text = diarize_and_merge(args.audio, segments,
                             min_speakers=args.min_speakers,
                             max_speakers=args.max_speakers)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[完成] → {args.out}", flush=True)
    else:
        print(text)


if __name__ == "__main__":
    main()

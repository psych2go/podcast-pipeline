"""
TTS module — Fish Audio API 调用 + 音频合并。

用法（库）:
    from tts import run_tts
    summary = run_tts(folder, briefing_file, merged_name)

用法（命令行）:
    python scripts/tts.py input.md [output_dir] [--speed 1.0]
"""
import sys
from pathlib import Path

# Ensure scripts/ is in sys.path for direct execution or when imported
_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import glob
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from tqdm import tqdm

from config import FISH_KEY, FISH_VOICE, FISH_MODEL
from validator import smart_chunk


API_URL = "https://api.fish.audio/v1/tts"
MAX_CHUNK_CHARS = 800
MAX_RETRIES = 5
DEFAULT_CONCURRENCY = 3
SECTION_SILENCE_SECONDS = 0.8  # 章节之间的静音时长


# ── 朗读前轻量归一 ─────────────────────────────────────────────────

def normalize_for_tts(text):
    """TTS 朗读前的轻量归一：清理不可见字符、冗余空白。保守，不改词义。"""
    # 不可见字符 / nbsp → 普通空格：
    #   零宽 U+200B-200F、行/段分隔 U+2028/2029、BOM/零宽不换行 U+FEFF、不间断空格 U+00A0
    text = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff\u00a0]", " ", text)
    # 行尾空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    # 3+ 换行折叠
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── 底层 API 调用 ─────────────────────────────────────────────────

def synth_chunk(client, text, speed=1.0):
    """调用 Fish Audio TTS API，返回音频 bytes。仅对可重试错误重试。"""
    body = {
        "text": text,
        "reference_id": FISH_VOICE,
        "format": "mp3",
        "mp3_bitrate": 128,
        "latency": "balanced",
        "normalize": True,
        "prosody": {"speed": speed, "volume": 0},
    }
    headers = {
        "Authorization": f"Bearer {FISH_KEY}",
        "Content-Type": "application/json",
        "model": FISH_MODEL,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.post(
                API_URL, headers=headers, json=body,
                timeout=httpx.Timeout(120, connect=30),
            )
            if r.status_code == 200:
                return r.content
            # 不可重试：鉴权/余额/资源/请求格式错误 → 立即抛出，别浪费重试
            if r.status_code in (401, 403):
                raise RuntimeError(f"Fish Audio 鉴权失败 ({r.status_code})")
            if r.status_code == 402:
                raise RuntimeError("Fish Audio 余额不足 (402)")
            if r.status_code == 404:
                raise RuntimeError("Fish Audio Voice ID 不存在 (404)")
            if r.status_code in (400, 422):
                raise RuntimeError(
                    f"Fish Audio 请求被拒 ({r.status_code}): {r.text[:120]}"
                )
            # 可重试：429 限流 / 5xx 服务端
            if r.status_code == 429:
                wait = 10 * attempt
                tqdm.write(f"  [TTS] 限流，等待 {wait}s")
                time.sleep(wait)
                continue
            last_err = f"HTTP {r.status_code}"
            tqdm.write(f"  [TTS] {last_err}，重试 {attempt}/{MAX_RETRIES}")
            time.sleep(3 * attempt)
        except httpx.HTTPError as e:
            # 网络错误 / 超时 → 可重试
            last_err = type(e).__name__
            tqdm.write(f"  [TTS] 网络异常 ({last_err})，重试 {attempt}/{MAX_RETRIES}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"TTS 重试 {MAX_RETRIES} 次后仍然失败: {last_err}")


def synth_chunks_concurrent(client, chunks, speed, concurrency):
    """并发合成多个 chunk，按输入顺序返回音频 bytes 列表。"""
    if concurrency <= 1 or len(chunks) <= 1:
        return [synth_chunk(client, c, speed) for c in chunks]

    results = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_to_idx = {
            ex.submit(synth_chunk, client, c, speed): i
            for i, c in enumerate(chunks)
        }
        for fut in as_completed(future_to_idx):
            results[future_to_idx[fut]] = fut.result()  # 失败会抛给上层
    return results


# ── 章节拆分 ───────────────────────────────────────────────────────

def split_sections(md_text):
    """
    按 ## 标题拆分 markdown，返回 [(title_or_None, body), ...]。
    第一个 ## 之前的引言段以 (None, body) 保留（不再被丢弃）。
    """
    parts = re.split(r"\n(?=## )", md_text)
    secs = []
    for idx, p in enumerate(parts):
        p = p.strip()
        if not p:
            continue
        if not p.startswith("## "):
            # 仅首段（preamble）保留；其余游离段忽略
            if idx == 0:
                secs.append((None, p))
            continue
        lines = p.split("\n", 1)
        secs.append((
            lines[0].replace("## ", "").strip(),
            lines[1] if len(lines) > 1 else "",
        ))
    return secs


def safe_filename(name, max_len=40):
    """将章节标题转为安全的文件名片段。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:max_len].strip()


# ── 音频合并（章节间插静音）─────────────────────────────────────────

def _probe_audio_params(mp3_path):
    """ffprobe 探测采样率/声道，返回 (sample_rate, channels) 或 None。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels",
             "-of", "csv=p=0", mp3_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(",")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None


def _make_silence(out_path, sample_rate, channels, seconds):
    """生成与章节音频同格式的静音 mp3，便于 ffmpeg -c copy 拼接。"""
    cl = "mono" if channels == 1 else "stereo"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"anullsrc=r={sample_rate}:cl={cl}",
             "-t", str(seconds),
             "-c:a", "libmp3lame", "-b:a", "128k",
             out_path],
            capture_output=True, timeout=30,
        )
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


def merge_mp3s(input_files, output_path, silence_seconds=SECTION_SILENCE_SECONDS):
    """
    合并 MP3，章节之间插入短静音。优先 ffmpeg concat（-c copy）。
    失败回退到裸字节拼接（无静音）。
    """
    sorted_files = sorted(input_files)
    if not sorted_files:
        return False

    # 尝试生成匹配格式的静音文件，用于章节间隔
    silence_path = None
    if silence_seconds and len(sorted_files) > 1:
        params = _probe_audio_params(sorted_files[0])
        if params:
            silence_path = output_path + ".silence.mp3"
            if not _make_silence(silence_path, params[0], params[1], silence_seconds):
                silence_path = None

    # Method 1: ffmpeg concat demuxer
    concat_list = None
    try:
        concat_list = output_path + ".concat.txt"
        with open(concat_list, "w") as f:
            for i, mp3 in enumerate(sorted_files):
                f.write(f"file '{mp3}'\n")
                if silence_path and i < len(sorted_files) - 1:
                    f.write(f"file '{silence_path}'\n")
        result = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0",
             "-i", concat_list, "-c", "copy", "-y", output_path],
            capture_output=True, timeout=600,
        )
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        tqdm.write(f"  [TTS] ffmpeg concat 失败 (rc={result.returncode})，回退裸拼接")
    except FileNotFoundError:
        tqdm.write("  [TTS] 未找到 ffmpeg，回退裸拼接")
    except (subprocess.CalledProcessError, OSError) as e:
        tqdm.write(f"  [TTS] ffmpeg 异常 ({e})，回退裸拼接")
    finally:
        if concat_list and os.path.exists(concat_list):
            os.unlink(concat_list)
        if silence_path and os.path.exists(silence_path):
            os.unlink(silence_path)

    # Method 2: 裸字节拼接（fallback，无章节静音）
    with open(output_path, "wb") as f:
        for mp3 in sorted_files:
            with open(mp3, "rb") as sf:
                f.write(sf.read())
    return True


# ── 主流程（库接口）─────────────────────────────────────────────────

def run_tts(folder, briefing_file, merged_name, speed=1.0,
            fresh=False, read_titles=True, concurrency=None):
    """
    为播客文件夹运行 TTS。

    参数:
        folder: 播客内容目录
        briefing_file: 讲稿文件名（如 "讲书稿.md"）
        merged_name: 合并音频文件名（不含 .mp3）
        speed: 语速，默认 1.0
        fresh: True 则清空旧音频重新生成；False（默认）断点续传
        read_titles: 是否在每节正文前朗读章节标题，默认 True
        concurrency: 单节内 chunk 并发数（默认读 TTS_CONCURRENCY 或 3）

    返回:
        结果描述字符串，如 "12.3MB, ~45min"
    """
    audio_dir = os.path.join(folder, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    # .tmp 永远是半成品，一律清掉
    for f in glob.glob(os.path.join(audio_dir, "*.tmp")):
        os.remove(f)

    briefing_path = os.path.join(folder, briefing_file)
    if not os.path.exists(briefing_path):
        print(f"  [TTS] 找不到 {briefing_file}", flush=True)
        return "NO FILE"

    txt = open(briefing_path, "r", encoding="utf-8").read()
    sections = split_sections(txt)

    # 计算每节目标文件名 + 要朗读的文本
    intended = []
    for idx, (title, body) in enumerate(sections):
        body = body.strip()
        if title is None:
            if len(body) < 5:
                continue
            fname = "00_开场.mp3"
            spoken = body
        else:
            fname = f"{idx:02d}_{safe_filename(title)}.mp3"
            spoken = (f"{title}。\n{body}").strip() if read_titles else body
        intended.append((fname, spoken))

    intended_names = {f for f, _ in intended}

    if fresh:
        for f in glob.glob(os.path.join(audio_dir, "*.mp3")):
            os.remove(f)
    else:
        # 断点续传：清掉不在目标集合里的孤儿 mp3，保留已完成的同名文件
        for f in glob.glob(os.path.join(audio_dir, "*.mp3")):
            if os.path.basename(f) not in intended_names:
                os.remove(f)

    if concurrency is None:
        try:
            concurrency = max(1, int(os.environ.get("TTS_CONCURRENCY", DEFAULT_CONCURRENCY)))
        except ValueError:
            concurrency = DEFAULT_CONCURRENCY

    total_kb = 0
    fatal = False
    # 源文件修改时间：断点续传时用于判断是否需要重录
    briefing_mtime = os.path.getmtime(briefing_path)
    with httpx.Client() as client:
        for fname, spoken in tqdm(intended, desc="[TTS] 章节", unit="个"):
            output_path = os.path.join(audio_dir, fname)

            # 断点续传：文件存在、非空、且不比源文件旧 → 跳过
            if not fresh and os.path.exists(output_path) \
                    and os.path.getsize(output_path) > 1024 \
                    and os.path.getmtime(output_path) >= briefing_mtime:
                size_kb = os.path.getsize(output_path) // 1024
                total_kb += size_kb
                tqdm.write(f"  [TTS] 跳过(已存在) {fname} ({size_kb}KB)")
                continue

            text = normalize_for_tts(spoken)
            if len(text) < 5:
                continue
            chunks = smart_chunk(text, max_chars=MAX_CHUNK_CHARS)
            tmp_path = output_path + ".tmp"

            try:
                ordered = synth_chunks_concurrent(client, chunks, speed, concurrency)
                with open(tmp_path, "wb") as f:
                    for audio in ordered:
                        f.write(audio)
                os.rename(tmp_path, output_path)
                size_kb = os.path.getsize(output_path) // 1024
                total_kb += size_kb
                tqdm.write(f"  [TTS] {fname} ({size_kb}KB, {len(chunks)}片)")
            except Exception as e:
                tqdm.write(f"  [TTS] 失败 {fname}: {e}")
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                if "401" in str(e) or "402" in str(e) or "403" in str(e):
                    fatal = True
                    break  # 致命错误，终止

            time.sleep(0.3)

    if fatal:
        return "ABORTED (鉴权/余额错误)"

    # 合并所有章节（章节间插静音）
    all_mp3s = sorted(glob.glob(os.path.join(audio_dir, "*.mp3")))
    if all_mp3s:
        merged_path = os.path.join(folder, f"{merged_name}.mp3")
        merge_mp3s(all_mp3s, merged_path)
        mb = os.path.getsize(merged_path) / 1024 / 1024
        approx_min = round(total_kb * 8 / 128 / 60)
        return f"{mb:.1f}MB, ~{approx_min}min"
    return "NO AUDIO"


# ── CLI 入口 ───────────────────────────────────────────────────────

def cli_main():
    import argparse as _argparse
    parser = _argparse.ArgumentParser(
        description="Fish Audio TTS - 将讲稿 markdown 转为分章节 mp3",
        usage="python scripts/tts.py input.md [output_dir] [--speed 1.0]"
    )
    parser.add_argument("input_md", nargs="?", help="讲稿 markdown 文件路径")
    parser.add_argument("output_dir", nargs="?", help="音频输出目录（默认 input.md 同级的 audio/ 目录）")
    parser.add_argument("--speed", type=float, default=1.0, help="语速（默认 1.0）")
    parser.add_argument("--fresh", action="store_true", help="清空旧音频重新生成（默认断点续传）")
    parser.add_argument("--no-titles", action="store_true", help="不在音频中朗读章节标题")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="单节内 chunk 并发数（默认读 TTS_CONCURRENCY 或 3）")

    parsed = parser.parse_args()
    speed = parsed.speed

    input_md = parsed.input_md or input("输入 MD 文件路径: ")
    out_dir = Path(parsed.output_dir) if parsed.output_dir else Path(input_md).parent / "audio"

    if not os.path.exists(input_md):
        print(f"❌ 找不到 {input_md}")
        sys.exit(1)

    print(f"\n处理: {Path(input_md).stem}")
    print("=" * 40)

    folder = Path(input_md).parent
    briefing_file = Path(input_md).name
    merged_name = Path(input_md).stem.replace("讲书稿", "").replace("简报", "").strip()
    if not merged_name:
        merged_name = Path(input_md).stem

    result = run_tts(
        str(folder), briefing_file, merged_name, speed,
        fresh=parsed.fresh,
        read_titles=not parsed.no_titles,
        concurrency=parsed.concurrency,
    )
    print(f"\n完成: {result}")


if __name__ == "__main__":
    cli_main()

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
import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Lock

import httpx
from tqdm import tqdm

from config import FISH_VOICE, FISH_MODEL, require_fish_key
from validator import smart_chunk


API_URL = "https://api.fish.audio/v1/tts"
MAX_CHUNK_CHARS = 800
MAX_RETRIES = 5
DEFAULT_CONCURRENCY = 3
SECTION_SILENCE_SECONDS = 0.8  # 章节之间的静音时长
TTS_MANIFEST_SCHEMA_VERSION = 1


@dataclass
class TTSResult:
    ok: bool
    summary: str
    expected_sections: int = 0
    completed_sections: int = 0
    failed_sections: list = field(default_factory=list)
    manifest_path: str = ""

    def __str__(self):
        return self.summary


@dataclass
class TTSUsage:
    api_requests: int = 0
    retry_count: int = 0
    synthesized_chunks: int = 0
    synthesized_characters: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record_attempt(self, retry=False):
        with self._lock:
            self.api_requests += 1
            if retry:
                self.retry_count += 1

    def record_chunks(self, chunks):
        with self._lock:
            self.synthesized_chunks += len(chunks)
            self.synthesized_characters += sum(len(chunk) for chunk in chunks)

    def as_dict(self):
        return {
            "api_requests": self.api_requests,
            "retry_count": self.retry_count,
            "synthesized_chunks": self.synthesized_chunks,
            "synthesized_characters": self.synthesized_characters,
        }


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_manifest(path, payload):
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def _load_manifest(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("schema_version") != TTS_MANIFEST_SCHEMA_VERSION:
        return {}
    return payload


def _section_fingerprint(text, speed, read_titles):
    payload = {
        "text": text,
        "voice": FISH_VOICE,
        "model": FISH_MODEL,
        "speed": speed,
        "read_titles": read_titles,
        "max_chunk_chars": MAX_CHUNK_CHARS,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def build_tts_plan(folder, briefing_file, speed=1.0, read_titles=True):
    briefing_path = Path(folder) / briefing_file
    if not briefing_path.exists():
        return []
    text = briefing_path.read_text(encoding="utf-8")
    sections = split_sections(text)
    tts_lexicon = load_tts_lexicon(folder)
    intended = []
    for idx, (title, body) in enumerate(sections):
        body = body.strip()
        if title is None:
            if len(body) < 5:
                continue
            filename = "00_开场.mp3"
            spoken = body
        else:
            filename = f"{idx:02d}_{safe_filename(title)}.mp3"
            spoken = (f"{title}。\n{body}").strip() if read_titles else body
        normalized = apply_tts_lexicon(
            normalize_for_tts(spoken), tts_lexicon)
        if len(normalized) < 5:
            continue
        intended.append({
            "filename": filename,
            "text": normalized,
            "fingerprint": _section_fingerprint(
                normalized, speed, read_titles),
        })
    return intended


def validate_tts_manifest(folder, briefing_file, merged_name):
    folder = Path(folder)
    manifest_path = folder / "tts_manifest.json"
    manifest = _load_manifest(manifest_path)
    errors = []
    if not manifest:
        return ["缺少或无法读取 tts_manifest.json"]
    if not manifest.get("completed"):
        errors.append("TTS manifest 未完成")
    if manifest.get("failed_sections"):
        errors.append(
            f"TTS manifest 存在失败章节: {manifest['failed_sections']}")

    config = manifest.get("config", {})
    if config.get("voice") != FISH_VOICE:
        errors.append("TTS 音色配置已变化")
    if config.get("model") != FISH_MODEL:
        errors.append("TTS 模型配置已变化")
    speed = config.get("speed")
    read_titles = config.get("read_titles")
    if not isinstance(speed, (int, float)) or not isinstance(read_titles, bool):
        errors.append("TTS manifest 缺少有效 speed/read_titles 配置")
        return errors

    current_plan = build_tts_plan(
        folder, briefing_file, speed=speed, read_titles=read_titles)
    manifest_sections = manifest.get("sections", [])
    section_by_name = {
        section.get("filename"): section
        for section in manifest_sections
        if isinstance(section, dict)
    }
    if manifest.get("expected_sections") != len(current_plan):
        errors.append("TTS 章节数量与当前讲稿不一致")
    for item in current_plan:
        section = section_by_name.get(item["filename"])
        if not section or section.get("status") != "complete":
            errors.append(f"TTS 章节未完成: {item['filename']}")
            continue
        if section.get("fingerprint") != item["fingerprint"]:
            errors.append(f"TTS 章节指纹已过期: {item['filename']}")

    final_path = folder / f"{merged_name}.mp3"
    final = manifest.get("final", {})
    if not final_path.exists():
        errors.append("最终 MP3 不存在")
    else:
        if final.get("size") != final_path.stat().st_size:
            errors.append("最终 MP3 大小与 TTS manifest 不一致")
        if final.get("sha256") != _sha256_file(final_path):
            errors.append("最终 MP3 哈希与 TTS manifest 不一致")
    return errors


def backfill_tts_manifest(
        folder, briefing_file, merged_name,
        speed=1.0, read_titles=True):
    """Create a manifest for verified existing section and final MP3 files."""
    folder = Path(folder)
    plan = build_tts_plan(
        folder, briefing_file, speed=speed, read_titles=read_titles)
    audio_dir = folder / "audio"
    final_path = folder / f"{merged_name}.mp3"
    missing = [
        item["filename"] for item in plan
        if not (audio_dir / item["filename"]).exists()
        or (audio_dir / item["filename"]).stat().st_size <= 1024
    ]
    if missing:
        raise RuntimeError(f"无法回填 TTS manifest，缺少章节: {missing}")
    if not final_path.exists() or final_path.stat().st_size <= 1024:
        raise RuntimeError("无法回填 TTS manifest，最终 MP3 不存在或体积异常")

    sections = []
    for item in plan:
        path = audio_dir / item["filename"]
        sections.append({
            "filename": item["filename"],
            "fingerprint": item["fingerprint"],
            "output_sha256": _sha256_file(path),
            "size": path.stat().st_size,
            "status": "complete",
            "cached": True,
        })
    manifest = {
        "schema_version": TTS_MANIFEST_SCHEMA_VERSION,
        "completed": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "briefing_file": briefing_file,
        "final_file": final_path.name,
        "config": {
            "voice": FISH_VOICE,
            "model": FISH_MODEL,
            "speed": speed,
            "read_titles": read_titles,
            "max_chunk_chars": MAX_CHUNK_CHARS,
        },
        "expected_sections": len(plan),
        "completed_sections": len(plan),
        "sections": sections,
        "failed_sections": [],
        "final": {
            "filename": final_path.name,
            "size": final_path.stat().st_size,
            "sha256": _sha256_file(final_path),
        },
        "backfilled": True,
    }
    manifest_path = folder / "tts_manifest.json"
    _write_manifest(manifest_path, manifest)
    return manifest_path


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


def load_tts_lexicon(folder):
    path = Path(folder) / "tts_lexicon.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("tts_lexicon.json 必须是字符串映射对象")
    result = {str(key): str(value) for key, value in data.items()}
    if any(not key for key in result):
        raise ValueError("tts_lexicon.json 不允许空原词")
    if any(not value for value in result.values()):
        raise ValueError("tts_lexicon.json 不允许空朗读文本")
    return result


def apply_tts_lexicon(text, lexicon):
    """一次性按长词优先替换，避免子串误替换和替换结果二次级联。"""
    if not lexicon:
        return text
    if any(not isinstance(source, str) or not source for source in lexicon):
        raise ValueError("TTS 词典原词必须是非空字符串")
    keys = sorted(lexicon, key=len, reverse=True)
    alternatives = []
    for source in keys:
        pattern = re.escape(source)
        if source[0].isascii() and source[0].isalnum():
            pattern = rf"(?<![A-Za-z0-9]){pattern}"
        if source[-1].isascii() and source[-1].isalnum():
            pattern = rf"{pattern}(?![A-Za-z0-9])"
        alternatives.append(pattern)
    matcher = re.compile("|".join(f"(?:{pattern})" for pattern in alternatives))
    return matcher.sub(lambda match: lexicon[match.group(0)], text)


# ── 底层 API 调用 ─────────────────────────────────────────────────

def synth_chunk(client, text, speed=1.0, usage=None):
    """调用 Fish Audio TTS API，返回音频 bytes。仅对可重试错误重试。"""
    fish_key = require_fish_key()
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
        "Authorization": f"Bearer {fish_key}",
        "Content-Type": "application/json",
        "model": FISH_MODEL,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        if usage is not None:
            usage.record_attempt(retry=attempt > 1)
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


def synth_chunks_concurrent(
        client, chunks, speed, concurrency, usage=None):
    """并发合成多个 chunk，按输入顺序返回音频 bytes 列表。"""
    if usage is not None:
        usage.record_chunks(chunks)
    if concurrency <= 1 or len(chunks) <= 1:
        return [synth_chunk(client, c, speed, usage) for c in chunks]

    results = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_to_idx = {
            ex.submit(synth_chunk, client, c, speed, usage): i
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
    使用 ffmpeg 合并 MP3，并在章节之间插入短静音。
    任一准备或合并步骤失败都返回 False，由上层保留旧最终音频。
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
                tqdm.write("  [TTS] 无法生成章节静音")
                return False
        else:
            tqdm.write("  [TTS] 无法探测章节音频参数")
            return False

    # Method 1: ffmpeg concat demuxer
    concat_list = None
    try:
        concat_list = output_path + ".concat.txt"
        with open(concat_list, "w") as f:
            for i, mp3 in enumerate(sorted_files):
                # concat demuxer uses single-quoted paths; escape apostrophes
                # in episode names so ffmpeg does not truncate the filename.
                escaped_mp3 = str(Path(mp3).resolve()).replace("\\", "\\\\").replace("'", "'\\''")
                f.write(f"file '{escaped_mp3}'\n")
                if silence_path and i < len(sorted_files) - 1:
                    escaped_silence = str(Path(silence_path).resolve()).replace("\\", "\\\\").replace("'", "'\\''")
                    f.write(f"file '{escaped_silence}'\n")
        result = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0",
             "-i", concat_list, "-c", "copy", "-y", output_path],
            capture_output=True, timeout=600,
        )
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        error = result.stderr.decode("utf-8", errors="replace").strip()
        tqdm.write(
            f"  [TTS] ffmpeg concat 失败 (rc={result.returncode}): "
            f"{error[-300:]}")
    except FileNotFoundError:
        tqdm.write("  [TTS] 未找到 ffmpeg")
    except (subprocess.CalledProcessError, OSError) as e:
        tqdm.write(f"  [TTS] ffmpeg 异常 ({e})")
    finally:
        if concat_list and os.path.exists(concat_list):
            os.unlink(concat_list)
        if silence_path and os.path.exists(silence_path):
            os.unlink(silence_path)

    return False


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
        TTSResult。任何章节失败时 ok=False，且不会覆盖最终 MP3。
    """
    audio_dir = os.path.join(folder, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    manifest_path = os.path.join(folder, "tts_manifest.json")

    # .tmp 永远是半成品，一律清掉
    for f in glob.glob(os.path.join(audio_dir, "*.tmp")):
        os.remove(f)
    merged_tmp = os.path.join(folder, f"{merged_name}.tmp.mp3")
    if os.path.exists(merged_tmp):
        os.remove(merged_tmp)

    briefing_path = os.path.join(folder, briefing_file)
    if not os.path.exists(briefing_path):
        print(f"  [TTS] 找不到 {briefing_file}", flush=True)
        return TTSResult(False, "NO FILE", manifest_path=manifest_path)

    intended = build_tts_plan(
        folder, briefing_file, speed=speed, read_titles=read_titles)

    intended_names = {item["filename"] for item in intended}
    if not intended:
        return TTSResult(
            False, "NO AUDIO", expected_sections=0, manifest_path=manifest_path)

    if fresh:
        for f in glob.glob(os.path.join(audio_dir, "*.mp3")):
            os.remove(f)
        if os.path.exists(manifest_path):
            os.remove(manifest_path)
    else:
        # 清掉不在目标集合里的孤儿 mp3；同名文件是否可复用由 manifest 决定。
        for f in glob.glob(os.path.join(audio_dir, "*.mp3")):
            if os.path.basename(f) not in intended_names:
                os.remove(f)

    if concurrency is None:
        try:
            concurrency = max(1, int(os.environ.get("TTS_CONCURRENCY", DEFAULT_CONCURRENCY)))
        except ValueError:
            concurrency = DEFAULT_CONCURRENCY

    previous = {} if fresh else _load_manifest(manifest_path)
    previous_sections = {
        item.get("filename"): item
        for item in previous.get("sections", [])
        if isinstance(item, dict) and item.get("filename")
    }
    manifest = {
        "schema_version": TTS_MANIFEST_SCHEMA_VERSION,
        "completed": False,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "briefing_file": briefing_file,
        "final_file": f"{merged_name}.mp3",
        "config": {
            "voice": FISH_VOICE,
            "model": FISH_MODEL,
            "speed": speed,
            "read_titles": read_titles,
            "max_chunk_chars": MAX_CHUNK_CHARS,
        },
        "expected_sections": len(intended),
        "sections": [],
        "failed_sections": [],
    }
    usage = TTSUsage()

    total_kb = 0
    with httpx.Client() as client:
        for item in tqdm(intended, desc="[TTS] 章节", unit="个"):
            fname = item["filename"]
            output_path = os.path.join(audio_dir, fname)
            previous_item = previous_sections.get(fname, {})
            cached = (
                not fresh
                and previous_item.get("status") == "complete"
                and previous_item.get("fingerprint") == item["fingerprint"]
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 1024
            )
            if cached:
                output_sha256 = _sha256_file(output_path)
                cached = previous_item.get("output_sha256") == output_sha256
            if cached:
                size = os.path.getsize(output_path)
                total_kb += size // 1024
                manifest["sections"].append({
                    "filename": fname,
                    "fingerprint": item["fingerprint"],
                    "output_sha256": output_sha256,
                    "size": size,
                    "status": "complete",
                    "cached": True,
                })
                tqdm.write(f"  [TTS] 跳过(指纹一致) {fname} ({size // 1024}KB)")
                continue

            chunks = smart_chunk(item["text"], max_chars=MAX_CHUNK_CHARS)
            tmp_path = output_path + ".tmp"

            try:
                ordered = synth_chunks_concurrent(
                    client, chunks, speed, concurrency, usage)
                with open(tmp_path, "wb") as f:
                    for audio in ordered:
                        f.write(audio)
                if os.path.getsize(tmp_path) <= 1024:
                    raise RuntimeError("生成的章节音频体积异常")
                os.replace(tmp_path, output_path)
                size = os.path.getsize(output_path)
                output_sha256 = _sha256_file(output_path)
                total_kb += size // 1024
                manifest["sections"].append({
                    "filename": fname,
                    "fingerprint": item["fingerprint"],
                    "output_sha256": output_sha256,
                    "size": size,
                    "status": "complete",
                    "cached": False,
                })
                tqdm.write(f"  [TTS] {fname} ({size // 1024}KB, {len(chunks)}片)")
            except Exception as e:
                tqdm.write(f"  [TTS] 失败 {fname}: {e}")
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                manifest["sections"].append({
                    "filename": fname,
                    "fingerprint": item["fingerprint"],
                    "status": "failed",
                    "error": str(e),
                })
                manifest["failed_sections"].append(fname)
                manifest["usage"] = usage.as_dict()
                _write_manifest(manifest_path, manifest)
                return TTSResult(
                    False,
                    f"ABORTED (章节失败: {fname})",
                    expected_sections=len(intended),
                    completed_sections=sum(
                        section.get("status") == "complete"
                        for section in manifest["sections"]
                    ),
                    failed_sections=[fname],
                    manifest_path=manifest_path,
                )

            time.sleep(0.3)

    # 只按本次 intended 顺序合并；任何缺失都阻断，绝不使用额外或旧文件。
    all_mp3s = [
        os.path.join(audio_dir, item["filename"])
        for item in intended
    ]
    missing = [path for path in all_mp3s if not os.path.exists(path)]
    if missing:
        manifest["failed_sections"] = [os.path.basename(path) for path in missing]
        manifest["usage"] = usage.as_dict()
        _write_manifest(manifest_path, manifest)
        return TTSResult(
            False,
            f"ABORTED (缺少章节音频: {manifest['failed_sections']})",
            expected_sections=len(intended),
            completed_sections=len(intended) - len(missing),
            failed_sections=manifest["failed_sections"],
            manifest_path=manifest_path,
        )

    merged_path = os.path.join(folder, f"{merged_name}.mp3")
    try:
        if not merge_mp3s(all_mp3s, merged_tmp):
            raise RuntimeError("音频合并失败")
        if not os.path.exists(merged_tmp) or os.path.getsize(merged_tmp) <= 1024:
            raise RuntimeError("合并后的音频体积异常")
        os.replace(merged_tmp, merged_path)
    except Exception as exc:
        if os.path.exists(merged_tmp):
            os.remove(merged_tmp)
        manifest["merge_error"] = str(exc)
        manifest["usage"] = usage.as_dict()
        _write_manifest(manifest_path, manifest)
        return TTSResult(
            False,
            f"ABORTED (合并失败: {exc})",
            expected_sections=len(intended),
            completed_sections=len(intended),
            manifest_path=manifest_path,
        )

    size = os.path.getsize(merged_path)
    manifest["completed"] = True
    manifest["completed_sections"] = len(intended)
    manifest["final"] = {
        "filename": os.path.basename(merged_path),
        "size": size,
        "sha256": _sha256_file(merged_path),
    }
    manifest["updated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["usage"] = usage.as_dict()
    _write_manifest(manifest_path, manifest)

    mb = size / 1024 / 1024
    approx_min = round(total_kb * 8 / 128 / 60)
    return TTSResult(
        True,
        f"{mb:.1f}MB, ~{approx_min}min",
        expected_sections=len(intended),
        completed_sections=len(intended),
        manifest_path=manifest_path,
    )


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
    parser.add_argument(
        "--backfill-manifest",
        action="store_true",
        help="不调用 TTS，仅为现有章节音频和最终 MP3 回填 manifest",
    )

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

    if parsed.backfill_manifest:
        result = backfill_tts_manifest(
            folder,
            briefing_file,
            merged_name,
            speed=speed,
            read_titles=not parsed.no_titles,
        )
    else:
        result = run_tts(
            str(folder), briefing_file, merged_name, speed,
            fresh=parsed.fresh,
            read_titles=not parsed.no_titles,
            concurrency=parsed.concurrency,
        )
    print(f"\n完成: {result}")


if __name__ == "__main__":
    cli_main()

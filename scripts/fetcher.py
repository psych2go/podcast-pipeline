"""
Transcript fetching: URL (multi-layer fallback) + local mp3 ASR.
"""
import json
import os
import re
import subprocess
import time

import httpx


# ── URL 抓取（四层降级）───────────────────────────────────────────

def fetch_transcript_from_url(url):
    """从网页 URL 抓取转录文本，四层降级策略"""
    print(f"[抓取] 目标: {url[:80]}...", flush=True)

    rss_result = _try_rss_transcript(url)
    if rss_result:
        print(f"[抓取] RSS 官方字幕成功，{len(rss_result)} 字符", flush=True)
        return rss_result

    html = _try_curl_cffi(url)
    if not html:
        html = _try_curl(url)
    if not html:
        html = _try_httpx(url)

    if html:
        text = _extract_with_trafilatura(html)
        if text and len(text) > 500:
            print(f"[抓取] trafilatura 提取成功，{len(text)} 字符", flush=True)
            return text

        text = _extract_with_regex(html)
        if text and len(text) > 500:
            print(f"[抓取] 正则提取成功，{len(text)} 字符", flush=True)
            return text

    text = _try_playwright(url)
    if text:
        print(f"[抓取] playwright 成功，{len(text)} 字符", flush=True)
        return text

    print("[抓取] 全部方法失败，请手动提供转录或 mp3 路径", flush=True)
    return None


def extract_title_from_url(url):
    """从网页 URL 提取 <title>，用于自动命名文件夹/音频。返回清理后的标题或 None。"""
    print(f"[标题] 尝试从 {url[:60]} 提取页面标题...", flush=True)
    html = _try_curl_cffi(url) or _try_curl(url) or _try_httpx(url)
    if not html:
        print("[标题] 抓取页面失败，无法自动命名", flush=True)
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        print("[标题] 未找到 <title>", flush=True)
        return None
    title = m.group(1)
    # HTML 实体
    title = (title.replace("&#39;", "'").replace("&apos;", "'")
             .replace("&quot;", '"').replace("&amp;", "&"))
    # 折叠空白
    title = re.sub(r"\s+", " ", title).strip()
    # 去常见平台后缀
    title = re.sub(r"\s*[-–—|]\s*YouTube\s*$", "", title, flags=re.IGNORECASE)
    # 去频道格式前缀（如 Naval 频道的 "Full Episode:"）
    title = re.sub(r"^Full Episode\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    # HappyScribe 等 transcript 站点：标题 — 节目名 Transcript
    title = re.sub(r"\s*[–—|]\s*[^–—|]*?Transcript\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\|\s*HappyScribe\s*$", "", title, flags=re.IGNORECASE)
    print(f"[标题] → {title}", flush=True)
    return title.strip() or None


def _try_rss_transcript(url):
    try:
        import feedparser
        known_feeds = {
            "all-in": "https://feeds.supercast.com/supercast_all_in_all",
            "naval": "https://nav.al/feed.xml",
        }
        for key, feed_url in known_feeds.items():
            if key in url.lower():
                feed = feedparser.parse(feed_url)
                for entry in feed.entries:
                    for link in entry.get("links", []):
                        if link.get("rel") == "transcript":
                            print(f"[抓取] RSS 发现字幕: {link['href'][:80]}", flush=True)
                            r = httpx.get(link["href"], timeout=30)
                            if r.status_code == 200:
                                return r.text
    except Exception as e:
        print(f"[抓取] RSS 尝试失败: {e}", flush=True)
    return None


def _try_curl_cffi(url):
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, impersonate="chrome", timeout=30)
        if r.status_code == 200 and len(r.text) > 1000:
            print(f"[抓取] curl_cffi 成功（HTTP {r.status_code}）", flush=True)
            return r.text
        print(f"[抓取] curl_cffi 返回 {r.status_code}", flush=True)
    except Exception as e:
        print(f"[抓取] curl_cffi 失败: {type(e).__name__}", flush=True)
    return None


def _try_curl(url):
    try:
        tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "podcast_page.html")
        subprocess.run(
            ["curl", "-sL", "--connect-timeout", "15", "--max-time", "30",
             "-H", "User-Agent: Mozilla/5.0", url, "-o", tmp],
            capture_output=True, timeout=45)
        if os.path.exists(tmp):
            html = open(tmp, "r", encoding="utf-8", errors="ignore").read()
            if len(html) > 1000:
                print(f"[抓取] curl 成功", flush=True)
                return html
    except Exception as e:
        print(f"[抓取] curl 失败: {e}", flush=True)
    return None


def _try_httpx(url):
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"},
                      timeout=30, follow_redirects=True)
        if r.status_code == 200 and len(r.text) > 1000:
            print(f"[抓取] httpx 成功", flush=True)
            return r.text
        print(f"[抓取] httpx 返回 {r.status_code}", flush=True)
    except Exception as e:
        print(f"[抓取] httpx 失败: {type(e).__name__}", flush=True)
    return None


def _extract_with_trafilatura(html):
    try:
        import trafilatura
        text = trafilatura.extract(
            html, include_comments=False, include_tables=False,
            favor_precision=True, output_format="txt")
        return text if text else None
    except Exception:
        return None


# 常见说话人标签标记：<strong>CHRIS WILLIAMSON:</strong> / <b>Alex:</b> / <h3>Host:</h3>
# 转成纯文本 "名字: "，避免 _extract_with_regex 把短标签剥掉（trafilatura 已能保留，这里兜底）
_SPEAKER_LABEL_RE = re.compile(
    r"<(?:strong|b|h[1-4])[^>]*>\s*([A-Za-z][A-Za-z .&'\-()]{1,40})\s*:\s*</(?:strong|b|h[1-4])>"
)


def _preserve_speaker_labels(html):
    """把 `<strong>名字:</strong>` 这类说话人标签还原为 `名字: ` 纯文本。"""
    return _SPEAKER_LABEL_RE.sub(r"\1: ", html)


def _extract_with_regex(html):
    html = _preserve_speaker_labels(html)
    texts = re.findall(r">([^<]{200,})<", html)
    speech = [t.strip() for t in texts
              if len(t.strip()) > 200
              and not t.strip().startswith(("@", "/*", "window", "(function",
                  "var ", "let ", "const ", ".font", "html", "{"))]
    return "\n\n".join(speech) if speech else None


def _try_playwright(url):
    try:
        from playwright.sync_api import sync_playwright
        transcripts = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            def on_response(resp):
                if "transcript" in resp.url.lower():
                    try:
                        transcripts.append(resp.json())
                    except Exception:
                        pass
            page.on("response", on_response)

            page.goto(url, wait_until="networkidle", timeout=60000)
            browser.close()

        if transcripts:
            for t in transcripts:
                text = _extract_text_from_json(t)
                if text and len(text) > 500:
                    return text

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)
            html = page.content()
            browser.close()

        return _extract_with_trafilatura(html) or _extract_with_regex(html)

    except ImportError:
        print("[抓取] playwright 未安装，跳过", flush=True)
    except Exception as e:
        print(f"[抓取] playwright 失败: {type(e).__name__}: {e}", flush=True)
    return None


def _extract_text_from_json(data):
    if isinstance(data, dict):
        for key in ["transcript", "text", "segments", "phrases", "words"]:
            if key in data:
                val = data[key]
                if isinstance(val, str) and len(val) > 200:
                    return val
                if isinstance(val, list):
                    texts = []
                    for item in val:
                        if isinstance(item, dict):
                            for tk in ["text", "content", "phrase"]:
                                if tk in item:
                                    texts.append(str(item[tk]))
                        elif isinstance(item, str):
                            texts.append(item)
                    result = " ".join(texts)
                    if len(result) > 200:
                        return result
        for v in data.values():
            result = _extract_text_from_json(v)
            if result:
                return result
    elif isinstance(data, list):
        texts = []
        for item in data:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                for tk in ["text", "content"]:
                    if tk in item:
                        texts.append(str(item[tk]))
        result = " ".join(texts)
        return result if len(result) > 200 else None
    return None


# ── ASR（本地 mp3 转录）───────────────────────────────────────────

# Whisper 质量预设（--asr-quality）
ASR_PRESETS = {
    "fast": {
        "model_size": "medium",
        "beam_size": 5,
        "desc": "中等模型，快速（~3×），适合清楚的长音频",
    },
    "balanced": {
        "model_size": "large-v3",
        "beam_size": 8,
        "desc": "large-v3 模型，质量优先，默认推荐",
    },
    "max": {
        "model_size": "large-v3",
        "beam_size": 12,
        "desc": "large-v3 + 宽波束 + 全抗幻觉阈值，最慢但最准",
    },
}


def make_initial_prompt(title=""):
    """从播客标题自动组装 initial_prompt，无需用户动手。

    专有名词错听是 Whisper 最大的弱点。initial_prompt 条件化解码器，
    让人名/术语在转录中保持一致。本函数从标题中提取有信息量的词，
    拼成一段提示文本，用户也可以手动传更全的内容。

    例：
      "The Future of AI, Chips, and Regulation"
      → "Podcast about The Future of AI, Chips, Regulation."
    """
    if not title or len(title) < 10:
        return None
    # 去掉文件名不安全字符（sanitize_title 已去掉的类）
    title = re.sub(r'[\\/:*?"<>|\[\]$`]', " ", title)
    # 去掉"播客""podcast""episode"等元词
    title = re.sub(
        r"\b(podcast|episode|full\s*episode|show|transcript|讲稿)\b",
        "", title, flags=re.IGNORECASE,
    )
    # 去尾部的站点名 "— YouTube" 等
    title = re.sub(r"\s*[-–—|]\s*\S+$", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) < 10:
        return None
    return f"Podcast about {title}."


# Whisper 在静音/音乐段常见的幻觉套话（大小写不敏感）
# 扩充自社区经验 + 常见播客场景
_HALLUCINATION_PATTERNS = [
    # YouTube/播客平台套话
    r"thank(s)?( you)? (for )?(watching|listening)\.?",
    r"thanks for (watching|listening)\.?",
    r"please (like and )?subscribe( to (the )?channel)?\.?",
    r"don't forget to (like and )?subscribe( to (the )?channel)?\.?",
    r"(make sure to )?(hit|click|ring) (the |that )(bell|notification|subscribe) (icon|button)?\.?",
    r"see you (in the |next |soon|next time)\.?",
    r"(and )?as always (thanks|thank you) for watching\.?",
    r"thanks again for watching\.?",
    r"i'll see you (in the |guys in |folks in )?(next|the next) (video|one|time)\.?",

    # Whisper 静音段常幻觉出 "[Music]" "[Applause]" 等标签
    r"\[music\]", r"\[applause\]", r"\[laughter\]",
    r"\[music\s*playing\]", r"\[background\s*noise\]",
    r"\[blank_?audio\]", r"\[silence\]",
    r"\[sound( effects?)?\]",
    r"^\s*(music|sound effect|background noise)\s*$",

    # 播客常见哼哈词（Whisper 常在静音间隙重复这些）
    r"^\s*(mm[ -]?hm|uh[ -]?huh|um|ah|oh|er|hmm)\s*$",

    # "from the [channel]" 类幻觉
    r"by the [A-Za-z]+ channel",
    r"(over at|at the) [A-Za-z]+ channel",

    # 语言检测幻觉（Whisper 在不确定时输出 "foreign"）
    r"\[foreign( language)?\]",
    r"\[speaking (foreign|in foreign language)\]",
    r"\[in [a-z]+(, please)?\]",

    # 赞助/广告插入类幻觉
    r"this (episode|video) is sponsored by",
    r"thanks to our (sponsors?|partners)",
    r"check (us )?out at (www\.)?\w+\.\w+",

    # 重复的问候/告别幻觉
    r"^hello(:|,)?( and)?( welcome)?$",
    r"^goodbye(:|,)?$",

    # 纯标点/符号行
    r"^[\s\-—·•*]+$",
]


def _remove_repeated_ngrams(text, max_repeat=2, ngram_size=5):
    """Whisper 有时在单词级别重复相同的 ngram（比整行重复更隐蔽）。
    检测连续重复的 n-gram（以词为单位），保留前 max_repeat 次。
    例如：["the", "the", "the", "the"] → ["the", "the"]
    """
    words = text.split()
    if len(words) < ngram_size * max_repeat:
        return text

    result = []
    i = 0
    while i < len(words):
        # 检查从 i 开始的 ngram 是否在 i+ngram_size 处重复
        ngram = words[i:i + ngram_size]
        repeat_count = 1
        j = i + ngram_size
        while j + ngram_size <= len(words) and words[j:j + ngram_size] == ngram:
            repeat_count += 1
            j += ngram_size
        if repeat_count > max_repeat:
            # 保留前 max_repeat 次
            keep = ngram * max_repeat
            result.extend(keep)
            i = j
        else:
            result.append(words[i])
            i += 1
    return " ".join(result)


def clean_whisper_hallucinations(text):
    """清洗 Whisper 幻觉：去已知套话 + 折叠连续重复 + 去 n-gram 重复。

    执行顺序：正则去套话 → 折叠整行重复 → 折叠 n-gram 重复 → 折叠空行。
    """
    # 1. 已知套话
    for pat in _HALLUCINATION_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    # 2. 连续重复行折叠：同一行连续出现 3 次及以上才视为幻觉（保留正常强调的两次）
    lines = text.split("\n")
    cleaned = []
    prev = None
    run = 0
    for ln in lines:
        s = ln.strip()
        if s and s == prev:
            run += 1
            if run >= 2:  # 第 3 次起丢弃
                continue
        else:
            run = 0
        cleaned.append(ln)
        prev = s

    text = "\n".join(cleaned)

    # 3. 单词级 n-gram 重复检测
    text = _remove_repeated_ngrams(text)

    # 4. 折叠多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 5. 行首尾空白清理
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def transcribe_mp3(mp3_path, model_size="large-v3", initial_prompt=None, hotwords=None,
                   beam_size=8, quality="balanced"):
    """用 faster-whisper 转录 mp3 文件。支持质量预设与精细调参。

    质量提升要点（专有名词是 Whisper 最大弱点，下面几个参数专门治这个）：
    - initial_prompt: 已知人名/公司名/术语/背景，条件化解码器，显著减少错听。
      例：'Podcast with Naval Ravikant, Gary Tan (Y Combinator), Farbod, Daniel.
           Topics: AI, Claude Code, Codex, AGI, startups.'
    - hotwords: 逗号分隔的热词，进一步加权特定词（faster-whisper 1.2+）。
      例：'Naval,Gary Tan,Y Combinator,Claude Code,Codex,Farbod'

    质量预设（--asr-quality）：
    - fast:    medium, beam=5，适合清楚音频快速出稿
    - balanced（默认）: large-v3, beam=8, VAD + 抗幻觉参数
    - max:     large-v3, beam=12, 最高精度 + 词级置信度过滤
    """
    # 用 quality preset 覆盖 model_size / beam_size（除非用户显式传了 --asr-model, beam_size 则合并）
    preset = ASR_PRESETS.get(quality, ASR_PRESETS["balanced"])
    if not beam_size or beam_size == 8:
        beam_size = preset["beam_size"]
    if quality != "fast":
        model_size = preset["model_size"]
    # quality="fast" 时保留用户指定的 model_size（默认 medium）

    print(f"[ASR] [{quality}] 用 {model_size} 转录 {os.path.basename(mp3_path)}"
          f" (beam={beam_size}"
          f"{', +initial_prompt' if initial_prompt else ''}"
          f"{', +hotwords' if hotwords else ''})...",
          flush=True)
    import faster_whisper
    model = faster_whisper.WhisperModel(model_size, device="cpu", compute_type="int8")
    t0 = time.time()

    # ── 公共参数（所有预设共享） ──
    kwargs = dict(
        language="en",
        beam_size=beam_size,
        # condition_on_previous_text 是 Whisper 幻觉级联的主因：
        # 前一段的幻觉文本作为输入喂给下一段，错误逐渐放大。
        # 社区共识：False 更安全。用 initial_prompt 和 hotwords 替代上下文效果更可靠。
        condition_on_previous_text=False,
        # 静音段检测：如果一段音频前有 >=2s 静音且 logprob 低 → 跳过
        hallucination_silence_threshold=2.0,
        # VAD 过滤：只对含语音的片段解码，跳过静音、音乐、广告
        vad_filter=True,
        # VAD 参数微调：播客场景降低 threshold，宁可多切不要混入非语音
        vad_parameters=dict(
            threshold=0.4,          # 默认 0.5，降低以捕获更弱的语音（如远场人声）
            min_speech_duration_ms=500,   # 最短语音段 0.5s
            min_silence_duration_ms=300,  # 最短静音段 0.3s（更快切回语音）
        ),
    )

    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    if hotwords:
        kwargs["hotwords"] = hotwords

    # ── 预设差异化参数 ──
    if quality == "fast":
        # 快速模式：用默认 temperature 序列（简单清楚音频够用）
        pass  # 继承公共参数
    elif quality == "balanced":
        # 平衡模式：加压缩比阈值防循环重复
        kwargs["compression_ratio_threshold"] = 2.0  # 比默认 2.4 更敏感
        kwargs["log_prob_threshold"] = -1.5           # 比默认 -1.0 略宽容
        kwargs["no_speech_threshold"] = 0.5           # 比默认 0.6 略严格
    elif quality == "max":
        # 最大质量模式：全参数调优
        kwargs["compression_ratio_threshold"] = 1.8   # 更敏感防循环
        kwargs["log_prob_threshold"] = -1.0            # 默认值，不过滤（max 模式信任模型）
        kwargs["no_speech_threshold"] = 0.4            # 更严格过滤非语音
        kwargs["temperature"] = [0.0, 0.2, 0.4, 0.6]  # 上限 0.6（默认 1.0），质量更高
        kwargs["word_timestamps"] = True               # 词级时间戳，用于后续对齐/校验
        # no_repeat_ngram_size 抑制循环重复
        kwargs["no_repeat_ngram_size"] = 4             # 禁止同一 4-gram 出现两次
        kwargs["patience"] = 1.5                       # 略高的 patience，让 beam search 更充分

    segments, info = model.transcribe(mp3_path, **kwargs)
    all_text = []
    for seg in segments:
        all_text.append(seg.text.strip())
    duration = time.time() - t0
    raw = "\n".join(all_text)
    result = clean_whisper_hallucinations(raw)
    removed = max(0, len(raw) - len(result))
    # 检测语言
    detected_lang = getattr(info, "language", "en")
    lang_prob = getattr(info, "language_probability", None)
    lang_info = f" ({lang_prob:.0%})" if lang_prob else ""
    print(f"[ASR] 完成，{len(result)} 字符（清洗幻觉 {removed} 字符）"
          f" [lang={detected_lang}{lang_info}]"
          f"，耗时 {duration:.0f}s",
          flush=True)
    return result

def transcribe_mp3_timestamped(mp3_path, model_size="large-v3", initial_prompt=None,
                                hotwords=None, beam_size=8, quality="balanced"):
    """用 faster-whisper + word_timestamps 返回 [{start,end,text}, ...]（供 diarize 对齐）。"""
    import faster_whisper
    # model_size 传 HF 标准模型名（faster-whisper 自动下载到 HF_HOME 缓存，
    # 默认 ~/.cache/huggingface）。如需用本地快照，直接传 snapshots 目录路径即可。
    model = faster_whisper.WhisperModel(model_size, device="cpu", compute_type="int8")
    t0 = time.time()
    kwargs = dict(
        language="en", beam_size=beam_size,
        condition_on_previous_text=False,
        hallucination_silence_threshold=2.0,
        vad_filter=True,
        vad_parameters=dict(threshold=0.4, min_speech_duration_ms=500, min_silence_duration_ms=300),
    )
    if initial_prompt: kwargs["initial_prompt"] = initial_prompt
    if hotwords: kwargs["hotwords"] = hotwords
    # word_timestamps 不需要开（开销大），段级时间戳 whisper 默认就有
    segments, info = model.transcribe(mp3_path, **kwargs)
    result = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            result.append({"start": seg.start, "end": seg.end, "text": text})
    duration = time.time() - t0
    detected = getattr(info, "language", "en")
    print(f"[ASR] 完成（带时间戳），{len(result)} 片段，耗时 {duration:.0f}s [lang={detected}]", flush=True)
    return result


def transcribe(mp3_path, engine="whisper", quality="balanced", asr_model=None,
               initial_prompt=None, hotwords=None, lm_path=None,
               diarize_audio=False, min_speakers=None, max_speakers=None):
    """统一 ASR 入口（v5+）：Whisper large-v3-turbo（默认），支持说话人分离。

    engine:
      - "whisper": faster-whisper large-v3-turbo（默认，推荐）
      - "whisper-fast": = whisper + quality=fast 快速模式

    diarize_audio: True 时跑 pyannote 说话人分离，输出 [SPEAKER_XX]: 文本
    """
    q = "fast" if engine == "whisper-fast" else quality
    if diarize_audio:
        segments = transcribe_mp3_timestamped(
            mp3_path, model_size=asr_model or "large-v3",
            initial_prompt=initial_prompt, hotwords=hotwords,
            quality=q)
        from diarize import diarize_and_merge
        return diarize_and_merge(
            mp3_path, segments,
            min_speakers=min_speakers, max_speakers=max_speakers)
    return transcribe_mp3(
        mp3_path, model_size=asr_model or "large-v3",
        initial_prompt=initial_prompt, hotwords=hotwords,
        quality=q)


def load_transcript_from_file(path):
    """从本地文件读取转录文本（兼容 .txt / .json / .srt）"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _extract_text_from_json(data)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

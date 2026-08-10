"""
Transcript fetching: URL (multi-layer fallback) + local mp3 ASR.
"""
import json
import os
import re
from html import unescape
from html.parser import HTMLParser
import subprocess
import time

import httpx

from playwright_runtime import playwright_launch_env


# ── URL 抓取（四层降级）───────────────────────────────────────────


class _PodscriptsParser(HTMLParser):
    """提取 Podscripts 的 transcript-text spans，过滤站点 CSS/JS/广告。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.group = None
        self.group_depth = 0
        self.capture = None
        self.capture_kind = None
        self.groups = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "") or ""
        if tag == "div" and "single-sentence" in classes and self.group is None:
            self.group = {"timestamp": None, "texts": []}
            self.group_depth = 1
            return
        if self.group is not None:
            self.group_depth += 1
        if tag == "span" and self.group is not None:
            if "pod_timestamp_indicator" in classes:
                self.capture = []
                self.capture_kind = "timestamp"
            elif "transcript-text" in classes:
                self.capture = []
                self.capture_kind = "text"

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.append(data)

    def handle_endtag(self, tag):
        if self.capture is not None and tag == "span":
            text = " ".join("".join(self.capture).split())
            if self.capture_kind == "timestamp":
                match = re.search(r"Starting point is\s+([0-9:]+)", text)
                if match:
                    self.group["timestamp"] = sum(
                        int(value) * 60 ** index
                        for index, value in enumerate(reversed(match.group(1).split(":")))
                    )
            elif self.capture_kind == "text" and text:
                self.group["texts"].append(text)
            self.capture = None
            self.capture_kind = None
        if self.group is not None:
            if tag == "div" and self.group_depth == 1:
                text = " ".join(self.group["texts"]).strip()
                if text:
                    self.groups.append({
                        "start": self.group["timestamp"],
                        "text": text,
                    })
                self.group = None
                self.group_depth = 0
            else:
                self.group_depth -= 1


def _extract_podscripts_segments(html):
    parser = _PodscriptsParser()
    parser.feed(html)
    segments = parser.groups
    for index, segment in enumerate(segments):
        next_start = segments[index + 1]["start"] if index + 1 < len(segments) else None
        segment["end"] = next_start
    return [segment for segment in segments if segment.get("text")]


def _render_source_segments(segments):
    return "\n\n".join(segment["text"] for segment in segments).strip()


def fetch_transcript_from_url(url, return_metadata=False):
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
        if "podscripts.co" in url.lower():
            segments = _extract_podscripts_segments(html)
            if segments and len(_render_source_segments(segments)) > 500:
                text = _render_source_segments(segments)
                print(f"[抓取] Podscripts 正文提取成功，{len(segments)} 段，{len(text)} 字符", flush=True)
                if return_metadata:
                    return {
                        "text": text,
                        "segments": segments,
                        "meta": {"timestamped": True, "extractor": "podscripts_transcript_text"},
                    }
                return text
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
    title = unescape(title)
    # 折叠空白
    title = re.sub(r"\s+", " ", title).strip()
    # 去常见平台后缀
    title = re.sub(r"\s*[-–—|]\s*YouTube\s*$", "", title, flags=re.IGNORECASE)
    # 去频道格式前缀（如 Naval 频道的 "Full Episode:"）
    title = re.sub(r"^Full Episode\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    # Podscripts 页面标题常带节目名前缀和站点后缀，只保留 episode 标题。
    if re.match(r"^All-In with\b", title, flags=re.IGNORECASE):
        title = re.sub(r"^All-In with.*?\s+-\s+", "", title, count=1, flags=re.IGNORECASE)
    title = re.sub(
        r"\s+(?:Transcript(?:\s+and\s+Discussion)?)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*\|\s*HappyScribe\s*$", "", title, flags=re.IGNORECASE)
    print(f"[标题] → {title}", flush=True)
    return title.strip() or None


def _slug_tokens(value):
    """将 URL/title 转成用于 episode 匹配的稳定 token 集合。"""
    value = re.sub(r"https?://", "", value.lower())
    tokens = re.findall(r"[a-z0-9]{3,}", value)
    ignored = {"www", "com", "html", "transcript", "episode", "podcast"}
    return {token for token in tokens if token not in ignored}


def _rss_entry_matches_url(entry, url):
    """RSS 只允许返回能和当前 URL 对上的 episode，绝不返回第一条。"""
    target = _slug_tokens(url)
    if not target:
        return False
    fields = [
        entry.get("title", ""),
        entry.get("id", ""),
        entry.get("guid", ""),
        entry.get("link", ""),
    ]
    for link in entry.get("links", []):
        fields.append(link.get("href", ""))
    candidate = set().union(*(_slug_tokens(value) for value in fields))
    overlap = target & candidate
    # URL slug 通常包含 3 个以上有意义 token；短标题则至少匹配两个。
    threshold = 2 if len(target) <= 5 else 3
    return len(overlap) >= threshold


def _try_rss_transcript(url):
    try:
        import feedparser
        known_feeds = {
            "all-in": "https://feeds.supercast.com/supercast_all_in_all",
            "naval": "https://nav.al/feed.xml",
        }
        for key, feed_url in known_feeds.items():
            if key not in url.lower():
                continue
            feed = feedparser.parse(feed_url)
            matches = [entry for entry in feed.entries if _rss_entry_matches_url(entry, url)]
            if not matches:
                print("[抓取] RSS 未找到与当前 URL 确认匹配的 episode，跳过 RSS", flush=True)
                return None
            for entry in matches:
                for link in entry.get("links", []):
                    if link.get("rel") != "transcript" or not link.get("href"):
                        continue
                    print(f"[抓取] RSS 匹配字幕: {link['href'][:80]}", flush=True)
                    r = httpx.get(link["href"], timeout=30, follow_redirects=True)
                    if r.status_code == 200 and len(r.text) > 200:
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
        # Podscripts has occasionally served an expired edge certificate.
        # Keep normal verification first; only use the explicit, source-scoped
        # fallback when the secure request itself cannot be established.
        if "podscripts.co" in url.lower():
            try:
                print("[抓取][警告] Podscripts 证书校验失败，使用降级抓取", flush=True)
                r = cffi_requests.get(
                    url, impersonate="chrome", timeout=30, verify=False)
                if r.status_code == 200 and len(r.text) > 1000:
                    print(
                        f"[抓取] curl_cffi 降级成功（HTTP {r.status_code}）",
                        flush=True)
                    return r.text
            except Exception as fallback_error:
                print(
                    f"[抓取] curl_cffi 降级失败: "
                    f"{type(fallback_error).__name__}",
                    flush=True)
    return None


def _try_curl(url):
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["curl", "-sL", "--connect-timeout", "15", "--max-time", "30",
                 "-H", "User-Agent: Mozilla/5.0", url, "-o", tmp_path],
                capture_output=True, timeout=45, check=False)
            if os.path.exists(tmp_path):
                html = open(tmp_path, "r", encoding="utf-8", errors="ignore").read()
                if len(html) > 1000:
                    print("[抓取] curl 成功", flush=True)
                    return html
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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
            browser = p.chromium.launch(
                headless=True, env=playwright_launch_env())
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
            browser = p.chromium.launch(
                headless=True, env=playwright_launch_env())
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




def detect_source_warnings(text):
    """标记网页转录中常见的编辑导语/链接尾巴，不在 faithful 模式静默删除。"""
    warnings = []
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        warnings.append("contains_urls")
    if re.search(r"editor[’']?s note|editorial note|full transcript|read the full transcript", text, re.IGNORECASE):
        warnings.append("contains_editorial_intro")
    if re.search(r"related articles|recommended|you may also like|transcript:?$", text, re.IGNORECASE | re.MULTILINE):
        warnings.append("contains_footer_or_recommendations")
    return warnings


def apply_content_policy(text, policy="faithful"):
    """对已清洗文本执行明确的编辑策略；默认 faithful 不删真实内容。"""
    if policy not in {"faithful", "no-ads", "summary-ready"}:
        raise ValueError(f"未知 content policy: {policy}")
    if policy == "faithful":
        return text
    lines = []
    for line in text.splitlines():
        value = line.strip()
        if policy in {"no-ads", "summary-ready"} and re.search(
                r"sponsored by|our sponsor|promo code|use code|check out .*\.com",
                value, re.IGNORECASE):
            continue
        if policy == "summary-ready" and re.search(r"https?://|www\.", value, re.IGNORECASE):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_plain_transcript(text, max_chars=1200):
    """将没有时间戳的网页/本地文本切成可整理的段落块，避免整期变成一个 unit。"""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    chunks = []
    current = ""
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?。！？])\s+", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars))
            elif not current:
                current = sentence
            elif len(current) + len(sentence) + 1 <= max_chars:
                current += " " + sentence
            else:
                chunks.append(current)
                current = sentence
        if current and len(current) >= max_chars * 0.8:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


# ── ASR（本地音频转录）────────────────────────────────────────────

# 质量预设。显式 --asr-model 优先于预设，避免参数“看似生效、实际被覆盖”。
ASR_PRESETS = {
    "fast": {
        "model_size": "medium",
        "beam_size": 5,
        "temperature": 0.0,
        "desc": "中等模型，快速出稿",
    },
    "balanced": {
        "model_size": "large-v3",
        "beam_size": 8,
        "temperature": 0.0,
        "desc": "large-v3，质量优先",
    },
    "max": {
        "model_size": "large-v3",
        "beam_size": 12,
        "temperature": [0.0, 0.2, 0.4, 0.6],
        "desc": "large-v3 + 更严格的抗幻觉设置",
    },
}


def resolve_asr_config(quality="balanced", model_size=None):
    """解析最终 ASR 配置；显式模型参数永远优先。"""
    from config import ASR_MODEL

    if quality not in ASR_PRESETS:
        raise ValueError(f"未知 ASR 质量预设: {quality}")
    preset = ASR_PRESETS[quality]
    return {
        "quality": quality,
        "model_size": model_size or ASR_MODEL or preset["model_size"],
        "beam_size": preset["beam_size"],
        "temperature": preset["temperature"],
    }


def make_initial_prompt(title=""):
    """从播客标题组装 initial_prompt，避免把平台元信息喂给 Whisper。"""
    if not title or len(title) < 10:
        return None
    title = re.sub(r'[\\/:*?"<>|\[\]$`]', " ", title)
    title = re.sub(
        r"\b(podcast|episode|full\s*episode|show|transcript|讲稿)\b",
        "", title, flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*[-–—|]\s*(youtube|happyscribe|singjupost)\s*$", "", title,
                   flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) < 10:
        return None
    return f"Podcast about {title}."


# 只包含“很可能是解码幻觉”的模式。真实广告/片尾是否删除由内容策略决定，
# 不在 ASR 层静默删除。
_HALLUCINATION_PATTERNS = [
    r"^\s*\[music\]\s*$",
    r"^\s*\[applause\]\s*$",
    r"^\s*\[laughter\]\s*$",
    r"^\s*\[background\s*noise\]\s*$",
    r"^\s*\[blank_?audio\]\s*$",
    r"^\s*\[silence\]\s*$",
    r"^\s*\[sound( effects?)?\]\s*$",
    r"^\s*\[foreign( language)?\]\s*$",
    r"^\s*(mm[ -]?hm|uh[ -]?huh|um|ah|er|hmm)\s*$",
    r"^\s*[\-—·•*]+\s*$",
]


def _remove_repeated_ngrams(text, max_repeat=2, ngram_size=5):
    """去除连续重复 n-gram；只处理明确的连续循环，避免误删正常重复。"""
    words = text.split()
    if len(words) < ngram_size * (max_repeat + 1):
        return text

    result = []
    i = 0
    while i < len(words):
        ngram = words[i:i + ngram_size]
        repeat_count = 1
        j = i + ngram_size
        while j + ngram_size <= len(words) and words[j:j + ngram_size] == ngram:
            repeat_count += 1
            j += ngram_size
        if repeat_count > max_repeat:
            result.extend(ngram * max_repeat)
            i = j
        else:
            result.append(words[i])
            i += 1
    return " ".join(result)


def clean_whisper_hallucinations(text):
    """清洗确定性 ASR 幻觉，不负责删除真实广告或节目内容。"""
    for pat in _HALLUCINATION_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE | re.MULTILINE)

    lines = text.splitlines()
    cleaned = []
    prev = None
    run = 0
    for line in lines:
        value = line.strip()
        if value and value == prev:
            run += 1
            if run >= 2:
                continue
        else:
            run = 0
        cleaned.append(line)
        prev = value

    text = "\n".join(cleaned)
    text = _remove_repeated_ngrams(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _model_path(model_size):
    """解析模型路径，不写死某台机器的用户目录。"""
    from config import ASR_MODEL_CACHE

    if os.path.isdir(model_size):
        return model_size
    if ASR_MODEL_CACHE:
        candidate = os.path.join(ASR_MODEL_CACHE, model_size)
        if os.path.isdir(candidate):
            return candidate
    return model_size


def _transcribe_kwargs(config, initial_prompt=None, hotwords=None, word_timestamps=True):
    kwargs = {
        "language": "en",
        "beam_size": config["beam_size"],
        "temperature": config["temperature"],
        # 避免前一段幻觉污染下一段；专有名词通过 prompt/hotwords 提供。
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 2.0,
        "vad_filter": True,
        "vad_parameters": {
            "threshold": 0.4,
            "min_speech_duration_ms": 300,
            "min_silence_duration_ms": 350,
        },
        "word_timestamps": word_timestamps,
    }
    if config["quality"] in ("balanced", "max"):
        kwargs.update({
            "compression_ratio_threshold": 2.0 if config["quality"] == "balanced" else 1.8,
            "log_prob_threshold": -1.5 if config["quality"] == "balanced" else -1.0,
            "no_speech_threshold": 0.5 if config["quality"] == "balanced" else 0.4,
        })
    if config["quality"] == "max":
        kwargs.update({"no_repeat_ngram_size": 4, "patience": 1.5})
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    if hotwords:
        kwargs["hotwords"] = hotwords
    return kwargs


def _word_to_dict(word):
    result = {
        "word": getattr(word, "word", ""),
        "start": getattr(word, "start", None),
        "end": getattr(word, "end", None),
    }
    probability = getattr(word, "probability", None)
    if probability is not None:
        result["probability"] = probability
    return result


def _segment_to_dict(segment):
    result = {
        "start": float(getattr(segment, "start", 0.0) or 0.0),
        "end": float(getattr(segment, "end", 0.0) or 0.0),
        "text": (getattr(segment, "text", "") or "").strip(),
    }
    for attr in ("avg_logprob", "compression_ratio", "no_speech_prob", "temperature"):
        value = getattr(segment, attr, None)
        if value is not None:
            result[attr] = value
    words = getattr(segment, "words", None)
    if words:
        result["words"] = [_word_to_dict(word) for word in words]
    return result


def _load_whisper_model(model_size):
    import faster_whisper
    from config import ASR_COMPUTE_TYPE, ASR_DEVICE

    resolved = _model_path(model_size)
    return faster_whisper.WhisperModel(
        resolved, device=ASR_DEVICE, compute_type=ASR_COMPUTE_TYPE)


def transcribe_mp3_timestamped(mp3_path, model_size=None, initial_prompt=None,
                                hotwords=None, beam_size=None, quality="balanced",
                                clean_hallucinations=True):
    """返回可审计的 ASR 片段和元数据。"""
    config = resolve_asr_config(quality, model_size)
    if beam_size is not None:
        config["beam_size"] = beam_size
    print(
        f"[ASR] [{quality}] model={config['model_size']} "
        f"beam={config['beam_size']} word_timestamps=true "
        f"file={os.path.basename(mp3_path)}",
        flush=True,
    )
    model = _load_whisper_model(config["model_size"])
    started = time.time()
    segments, info = model.transcribe(
        mp3_path,
        **_transcribe_kwargs(config, initial_prompt, hotwords, word_timestamps=True),
    )

    result = []
    raw_chars = 0
    removed_chars = 0
    for segment in segments:
        item = _segment_to_dict(segment)
        raw_text = item["text"]
        raw_chars += len(raw_text)
        item["text"] = clean_whisper_hallucinations(raw_text) if clean_hallucinations else raw_text
        removed_chars += max(0, len(raw_text) - len(item["text"]))
        if item["text"]:
            result.append(item)

    language = getattr(info, "language", None)
    language_probability = getattr(info, "language_probability", None)
    meta = {
        "model": config["model_size"],
        "quality": quality,
        "beam_size": config["beam_size"],
        "language": language,
        "language_probability": language_probability,
        "elapsed_seconds": round(time.time() - started, 2),
        "raw_chars": raw_chars,
        "removed_hallucination_chars": removed_chars,
        "segment_count": len(result),
    }
    print(
        f"[ASR] 完成，{len(result)} 片段，{sum(len(x['text']) for x in result)} 字符，"
        f"清洗 {removed_chars} 字符，耗时 {meta['elapsed_seconds']:.0f}s",
        flush=True,
    )
    return {"segments": result, "meta": meta}


def render_segments(segments, include_timestamps=False):
    lines = []
    for segment in segments:
        text = segment.get("text", "").strip()
        if not text:
            continue
        prefix = ""
        if include_timestamps:
            prefix = f"[{segment.get('start', 0):.2f}-{segment.get('end', 0):.2f}] "
        if segment.get("speaker"):
            prefix += f"[{segment['speaker']}] "
        lines.append(prefix + text)
    return "\n".join(lines).strip()


def transcribe_mp3(mp3_path, model_size=None, initial_prompt=None, hotwords=None,
                   beam_size=None, quality="balanced"):
    """兼容旧调用方：返回纯文本，但内部使用统一的结构化 ASR。"""
    result = transcribe_mp3_timestamped(
        mp3_path, model_size=model_size, initial_prompt=initial_prompt,
        hotwords=hotwords, beam_size=beam_size, quality=quality)
    return render_segments(result["segments"])


def transcribe(mp3_path, engine="whisper", quality="balanced", asr_model=None,
               initial_prompt=None, hotwords=None, lm_path=None,
               diarize_audio=False, min_speakers=None, max_speakers=None,
               return_metadata=False):
    """统一 ASR 入口。engine 保留兼容性，实际质量由 quality/model 控制。"""
    if engine not in ("whisper", "whisper-fast"):
        raise ValueError(f"不支持的 ASR 引擎: {engine}")
    effective_quality = "fast" if engine == "whisper-fast" else quality
    result = transcribe_mp3_timestamped(
        mp3_path,
        model_size=asr_model,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        quality=effective_quality,
    )

    if diarize_audio:
        from diarize import diarize_and_merge
        result["segments"] = diarize_and_merge(
            mp3_path,
            result["segments"],
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            return_segments=True,
        )
        result["meta"]["diarization"] = True
        result["meta"]["segment_count"] = len(result["segments"])
        result["meta"]["speaker_count"] = len({
            segment.get("speaker") for segment in result["segments"]
            if segment.get("speaker")
        })
    else:
        result["meta"]["diarization"] = False

    result["text"] = render_segments(result["segments"])
    return result if return_metadata else result["text"]


def load_transcript_from_file(path):
    """从本地文件读取转录文本（兼容 .txt / .json / .srt）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "segments" in data:
            return render_segments(data["segments"])
        return _extract_text_from_json(data)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

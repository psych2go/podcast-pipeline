"""
Transcript fetching: URL (multi-layer fallback) + local mp3 ASR.
"""
import json
import os
import re
import ssl
from html import unescape
from html.parser import HTMLParser
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

import httpx

from asr_refinement import AsrContext, build_asr_context, refine_segments
from config import FETCH_MAX_RETRIES, FETCH_TIMEOUT, API_RETRY_BACKOFF
from playwright_runtime import playwright_launch_env
from retry import exponential_delay, retry_after_seconds
from sources import source_host


# ── URL 抓取（四层降级）───────────────────────────────────────────

_HTML_CACHE = {}
_HTML_CACHE_TTL_SECONDS = 120


def _timestamp_seconds(text):
    match = re.search(
        r"(?:(?:Starting point is|Starts at|Begins at)\s+)?"
        r"([0-9]{1,2}(?::[0-9]{2}){1,2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return sum(
        int(value) * 60 ** index
        for index, value in enumerate(reversed(match.group(1).split(":")))
    )


class _PodscriptsParser(HTMLParser):
    """提取 Podscripts 的 transcript-text spans，过滤站点 CSS/JS/广告。"""

    VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

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
        if tag == "br" and self.capture is not None:
            self.capture.append(" ")
        if tag == "div" and "single-sentence" in classes and self.group is None:
            self.group = {"timestamp": None, "texts": []}
            self.group_depth = 1
            return
        if self.group is not None and tag not in self.VOID_TAGS:
            self.group_depth += 1
        if tag == "span" and self.group is not None:
            if "pod_timestamp_indicator" in classes:
                self.capture = []
                self.capture_kind = "timestamp"
            elif "transcript-text" in classes:
                self.capture = []
                self.capture_kind = "text"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.append(data)

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS:
            return
        if self.capture is not None and tag == "span":
            text = " ".join("".join(self.capture).split())
            if self.capture_kind == "timestamp":
                self.group["timestamp"] = _timestamp_seconds(text)
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


def _plain_transcript_result(text, extractor, fetch_meta=None):
    return {
        "text": text,
        "segments": [
            {
                "start": None,
                "end": None,
                "text": chunk,
                "synthetic_boundary": True,
            }
            for chunk in chunk_plain_transcript(text)
        ],
        "meta": {
            "timestamped": False,
            "extractor": extractor,
            **(fetch_meta or {}),
        },
    }


def fetch_transcript_from_url(url, return_metadata=False):
    """从网页 URL 抓取转录文本，四层降级策略"""
    print(f"[抓取] 目标: {url[:80]}...", flush=True)

    rss_result = _try_rss_transcript(url)
    if rss_result:
        print(f"[抓取] RSS 官方字幕成功，{len(rss_result)} 字符", flush=True)
        if return_metadata:
            return _plain_transcript_result(
                rss_result, "rss_transcript")
        return rss_result

    html, fetch_meta = _fetch_html(url)

    if html:
        if source_host(url) == "podscripts.co":
            segments = _extract_podscripts_segments(html)
            if segments and len(_render_source_segments(segments)) > 500:
                text = _render_source_segments(segments)
                timestamp_count = sum(
                    segment.get("start") is not None for segment in segments)
                timestamp_coverage = timestamp_count / len(segments)
                timestamped = timestamp_count == len(segments)
                if not timestamp_count:
                    extractor = "podscripts_transcript_text_no_timestamps"
                elif not timestamped:
                    extractor = "podscripts_transcript_text_partial_timestamps"
                else:
                    extractor = "podscripts_transcript_text"
                print(f"[抓取] Podscripts 正文提取成功，{len(segments)} 段，{len(text)} 字符", flush=True)
                if return_metadata:
                    return {
                        "text": text,
                        "segments": segments,
                        "meta": {
                            "timestamped": timestamped,
                            "timestamp_coverage": round(
                                timestamp_coverage, 4),
                            "extractor": extractor,
                            **fetch_meta,
                        },
                    }
                return text
        text = _extract_with_trafilatura(html)
        if text and len(text) > 500:
            print(f"[抓取] trafilatura 提取成功，{len(text)} 字符", flush=True)
            if return_metadata:
                return _plain_transcript_result(
                    text, "trafilatura", fetch_meta)
            return text

        text = _extract_with_regex(html)
        if text and len(text) > 500:
            print(f"[抓取] 正则提取成功，{len(text)} 字符", flush=True)
            if return_metadata:
                return _plain_transcript_result(
                    text, "regex", fetch_meta)
            return text

    text = _try_playwright(url)
    if text:
        print(f"[抓取] playwright 成功，{len(text)} 字符", flush=True)
        if return_metadata:
            return _plain_transcript_result(text, "playwright")
        return text

    print("[抓取] 全部方法失败，请手动提供转录或 mp3 路径", flush=True)
    return None


def extract_title_from_url(url):
    """从网页 URL 提取 <title>，用于自动命名文件夹/音频。返回清理后的标题或 None。"""
    print(f"[标题] 尝试从 {url[:60]} 提取页面标题...", flush=True)
    html, _fetch_meta = _fetch_html(url)
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
    """Return distinctive episode tokens; URL hosts and podcast paths are ignored."""
    text = str(value or "").casefold()
    if text.startswith(("http://", "https://")):
        path = urlsplit(text).path.rstrip("/")
        text = path.rsplit("/", 1)[-1]
    tokens = re.findall(r"[a-z0-9]{3,}", text)
    ignored = {
        "www", "com", "html", "transcript", "episode", "podcast",
        "the", "and", "with", "for", "from", "into", "about", "this",
        "that", "state", "future", "full",
    }
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
    if len(target) <= 3:
        return len(overlap) == len(target) and len(overlap) >= 2
    return len(overlap) >= 3 and len(overlap) / len(target) >= 0.6


_KNOWN_PODCAST_FEEDS = {
    "all-in": "https://feeds.supercast.com/supercast_all_in_all",
    "huberman-lab": "https://feeds.megaphone.fm/hubermanlab",
    "naval": "https://nav.al/feed.xml",
}


def discover_official_episode_url(url):
    """Return an RSS-matched official episode URL, or None when uncertain."""
    try:
        import feedparser
    except ImportError:
        return None
    source = str(url or "")
    for key, feed_url in _KNOWN_PODCAST_FEEDS.items():
        if key not in source.casefold():
            continue
        try:
            feed = feedparser.parse(feed_url)
            matches = [
                entry for entry in feed.entries
                if _rss_entry_matches_url(entry, source)
            ]
        except Exception:
            return None
        if len(matches) != 1:
            return None
        entry = matches[0]
        candidates = [entry.get("link", "")]
        candidates.extend(
            link.get("href", "")
            for link in entry.get("links", [])
            if link.get("rel") in {"alternate", None, ""}
        )
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if (
                    candidate.startswith(("http://", "https://"))
                    and source_host(candidate) != "podscripts.co"):
                return candidate
        return None
    return None


def _try_rss_transcript(url):
    try:
        import feedparser
        for key, feed_url in _KNOWN_PODCAST_FEEDS.items():
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


def _is_certificate_verification_error(error):
    if isinstance(error, ssl.SSLCertVerificationError):
        return True
    try:
        from curl_cffi.requests.exceptions import CertificateVerifyError
    except ImportError:
        return False
    return isinstance(error, CertificateVerifyError)


def _try_curl_cffi_with_metadata(url):
    metadata = {"transport": "curl_cffi"}
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(
            url, impersonate="chrome", timeout=FETCH_TIMEOUT)
        if r.status_code == 200 and len(r.text) > 1000:
            print(f"[抓取] curl_cffi 成功（HTTP {r.status_code}）", flush=True)
            return r.text, {"transport": "curl_cffi", "tls_downgrade": False}
        print(f"[抓取] curl_cffi 返回 {r.status_code}", flush=True)
        metadata["status_code"] = r.status_code
        if r.status_code == 429:
            metadata["retry_after"] = retry_after_seconds(
                r.headers.get("retry-after"))
    except Exception as e:
        print(f"[抓取] curl_cffi 失败: {type(e).__name__}", flush=True)
        metadata["last_error"] = type(e).__name__
        if (
                source_host(url) == "podscripts.co"
                and _is_certificate_verification_error(e)):
            try:
                print("[抓取][警告] Podscripts 证书校验失败，使用降级抓取", flush=True)
                r = cffi_requests.get(
                    url, impersonate="chrome", timeout=FETCH_TIMEOUT,
                    verify=False)
                if r.status_code == 200 and len(r.text) > 1000:
                    print(
                        f"[抓取] curl_cffi 降级成功（HTTP {r.status_code}）",
                        flush=True)
                    return r.text, {
                        "transport": "curl_cffi",
                        "tls_downgrade": True,
                        "tls_downgrade_reason": (
                            f"{type(e).__name__}: {str(e)[:300]}"
                        ),
                    }
            except Exception as fallback_error:
                print(
                    f"[抓取] curl_cffi 降级失败: "
                    f"{type(fallback_error).__name__}",
                    flush=True)
                metadata["last_error"] = type(fallback_error).__name__
    return None, metadata


def _try_curl_cffi(url):
    html, _metadata = _try_curl_cffi_with_metadata(url)
    return html


def _fetch_html(url):
    cached = _HTML_CACHE.get(url)
    if cached and time.monotonic() - cached["stored_at"] < _HTML_CACHE_TTL_SECONDS:
        return cached["html"], dict(cached["meta"])

    last_metadata = {}
    last_attempt = 0
    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        last_attempt = attempt
        html, cffi_metadata = _try_curl_cffi_with_metadata(url)
        metadata = cffi_metadata
        if not html:
            html = _try_curl(url)
            if html:
                metadata = {"transport": "curl"}
        if not html:
            html, httpx_metadata = _try_httpx_with_metadata(url)
            if html or httpx_metadata:
                metadata = httpx_metadata
        if html:
            metadata = dict(metadata)
            metadata["fetch_retry_count"] = attempt - 1
            _HTML_CACHE[url] = {
                "html": html,
                "meta": metadata,
                "stored_at": time.monotonic(),
            }
            return html, metadata
        last_metadata = dict(metadata or last_metadata)
        status_code = last_metadata.get("status_code")
        if (
                isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in {408, 425, 429}):
            break
        if attempt == FETCH_MAX_RETRIES:
            break
        wait = max(
            exponential_delay(attempt, API_RETRY_BACKOFF),
            last_metadata.get("retry_after") or 0.0,
        )
        print(
            f"[抓取] 所有 HTTP transport 均失败，{wait:g}s 后重试 "
            f"{attempt}/{FETCH_MAX_RETRIES}",
            flush=True,
        )
        time.sleep(wait)
    last_metadata["fetch_retry_count"] = max(0, last_attempt - 1)
    return None, last_metadata


def _try_curl(url):
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["curl", "-sL", "--connect-timeout",
                 str(min(15, FETCH_TIMEOUT)), "--max-time", str(FETCH_TIMEOUT),
                 "-H", "User-Agent: Mozilla/5.0", url, "-o", tmp_path],
                capture_output=True, timeout=FETCH_TIMEOUT + 15, check=False)
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


def _try_httpx_with_metadata(url):
    metadata = {"transport": "httpx"}
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"},
                      timeout=FETCH_TIMEOUT, follow_redirects=True)
        if r.status_code == 200 and len(r.text) > 1000:
            print(f"[抓取] httpx 成功", flush=True)
            return r.text, metadata
        print(f"[抓取] httpx 返回 {r.status_code}", flush=True)
        metadata["status_code"] = r.status_code
        if r.status_code == 429:
            metadata["retry_after"] = retry_after_seconds(
                r.headers.get("retry-after"))
    except Exception as e:
        print(f"[抓取] httpx 失败: {type(e).__name__}", flush=True)
        metadata["last_error"] = type(e).__name__
    return None, metadata


def _try_httpx(url):
    html, _metadata = _try_httpx_with_metadata(url)
    return html


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
        "model_size": "large-v3-turbo",
        "beam_size": 8,
        "temperature": 0.0,
        "desc": "large-v3-turbo，参考集验证的默认策略",
    },
    "max": {
        "model_size": "large-v3",
        "beam_size": 12,
        "temperature": [0.0, 0.2, 0.4, 0.6],
        "desc": "large-v3 + 更严格的抗幻觉设置",
    },
}


def preset_model_policy():
    """Return the production default model for each quality preset."""
    return {
        name: config["model_size"]
        for name, config in ASR_PRESETS.items()
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
    return build_asr_context(title=title).initial_prompt


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
    """Preserve decoder speech; ambiguous cleanup belongs in correction review.

    The pre-normalization value is stored per segment. This compatibility
    helper now only normalizes trailing whitespace and never deletes fillers,
    foreign-language markers, repetition, advertisements, or banter.
    """
    return re.sub(r"[ \t]+\n", "\n", str(text or "")).strip()


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


def _transcribe_kwargs(
        config, initial_prompt=None, hotwords=None, word_timestamps=True,
        language="en"):
    kwargs = {
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
    if language:
        kwargs["language"] = language
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
    decoder_text = getattr(segment, "text", "") or ""
    result = {
        "start": float(getattr(segment, "start", 0.0) or 0.0),
        "end": float(getattr(segment, "end", 0.0) or 0.0),
        "decoder_text": decoder_text,
        "text": decoder_text.strip(),
    }
    for attr in ("avg_logprob", "compression_ratio", "no_speech_prob", "temperature"):
        value = getattr(segment, attr, None)
        if value is not None:
            result[attr] = value
    words = getattr(segment, "words", None)
    if words:
        result["words"] = [_word_to_dict(word) for word in words]
    return result


def _load_whisper_model(model_size, runtime=None):
    from config import ASR_COMPUTE_TYPE, ASR_DEVICE

    if runtime is None:
        from asr_runtime import resolve_runtime
        runtime = resolve_runtime(ASR_DEVICE, ASR_COMPUTE_TYPE)
    import faster_whisper

    resolved = _model_path(model_size)
    return faster_whisper.WhisperModel(
        resolved,
        device=runtime.device,
        compute_type=runtime.compute_type,
    )


def _offset_segment_timestamps(segment, offset):
    segment["start"] = float(segment.get("start", 0.0) or 0.0) + offset
    segment["end"] = float(segment.get("end", 0.0) or 0.0) + offset
    for word in segment.get("words", []):
        if word.get("start") is not None:
            word["start"] = float(word["start"]) + offset
        if word.get("end") is not None:
            word["end"] = float(word["end"]) + offset
    return segment


def _transcribe_audio_range(
        model, audio_path, start, end, model_size, context, language):
    """Decode one exact audio range with max parameters and global timestamps."""
    duration = max(0.01, float(end) - float(start))
    max_config = resolve_asr_config("max", model_size)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            temp_path = tmp.name
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-ss", f"{float(start):.3f}",
                "-i", audio_path,
                "-t", f"{duration:.3f}",
                "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le",
                temp_path,
            ],
            capture_output=True,
            check=True,
            timeout=max(60, min(600, round(duration * 8 + 30))),
        )
        decoded, _info = model.transcribe(
            temp_path,
            **_transcribe_kwargs(
                max_config,
                context.initial_prompt,
                context.hotwords,
                word_timestamps=True,
                language=language,
            ),
        )
        result = []
        for segment in decoded:
            item = _segment_to_dict(segment)
            decoder_text = item["decoder_text"]
            item["text"] = clean_whisper_hallucinations(decoder_text)
            item["normalization"] = {
                "changed": item["text"] != decoder_text,
                "operations": (
                    [{"type": "whitespace_normalization"}]
                    if item["text"] != decoder_text else []
                ),
            }
            if item["text"]:
                result.append(_offset_segment_timestamps(item, float(start)))
        return result
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def transcribe_mp3_timestamped(mp3_path, model_size=None, initial_prompt=None,
                                hotwords=None, beam_size=None, quality="balanced",
                                clean_hallucinations=True, language="en",
                                asr_context=None, adaptive_refinement=True):
    """返回可审计的 ASR 片段和元数据。"""
    config = resolve_asr_config(quality, model_size)
    context = asr_context or build_asr_context(
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )
    if not isinstance(context, AsrContext):
        raise TypeError("asr_context 必须是 AsrContext")
    requested_language = language
    if beam_size is not None:
        config["beam_size"] = beam_size
    print(
        f"[ASR] [{quality}] model={config['model_size']} "
        f"beam={config['beam_size']} word_timestamps=true "
        f"file={os.path.basename(mp3_path)}",
        flush=True,
    )
    from config import ASR_COMPUTE_TYPE, ASR_DEVICE
    from asr_runtime import resolve_runtime

    runtime = resolve_runtime(ASR_DEVICE, ASR_COMPUTE_TYPE)
    model = _load_whisper_model(config["model_size"], runtime=runtime)
    started = time.time()
    segments, info = model.transcribe(
        mp3_path,
        **_transcribe_kwargs(
            config,
            context.initial_prompt,
            context.hotwords,
            word_timestamps=True,
            language=language),
    )

    result = []
    raw_chars = 0
    removed_chars = 0
    for segment in segments:
        item = _segment_to_dict(segment)
        raw_text = item["decoder_text"]
        raw_chars += len(raw_text)
        item["text"] = clean_whisper_hallucinations(raw_text) if clean_hallucinations else raw_text.strip()
        changed = item["text"] != raw_text
        item["normalization"] = {
            "changed": changed,
            "operations": (
                [{"type": "whitespace_normalization"}] if changed else []
            ),
        }
        removed_chars += max(0, len(raw_text) - len(item["text"]))
        if item["text"]:
            result.append(item)

    refinement_meta = {
        "enabled": False,
        "reason": (
            "disabled"
            if not adaptive_refinement
            else "fast_quality"
            if quality == "fast"
            else "not_run"
        ),
    }
    if adaptive_refinement and quality in {"balanced", "max"}:
        from config import ASR_REFINE_MAX_RANGES

        print("[ASR] 评估困难片段并执行定向重解码...", flush=True)
        refined = refine_segments(
            result,
            lambda start, end, active_context: _transcribe_audio_range(
                model,
                mp3_path,
                start,
                end,
                config["model_size"],
                active_context,
                language,
            ),
            context,
            max_ranges=ASR_REFINE_MAX_RANGES,
        )
        result = refined["segments"]
        refinement_meta = refined["meta"]
        print(
            f"[ASR] 定向重解码 {refinement_meta['candidate_ranges']} 段，"
            f"接受 {refinement_meta['accepted_ranges']} 段，"
            f"剩余待复核 {refinement_meta['remaining_segments']} 段",
            flush=True,
        )

    detected_language = getattr(info, "language", None)
    language_probability = getattr(info, "language_probability", None)
    meta = {
        "engine": "faster-whisper",
        "model": config["model_size"],
        "quality": quality,
        "beam_size": config["beam_size"],
        "device": runtime.device,
        "compute_type": runtime.compute_type,
        "language": detected_language,
        "language_probability": language_probability,
        "requested_language": requested_language or "auto",
        "elapsed_seconds": round(time.time() - started, 2),
        "raw_chars": raw_chars,
        "removed_hallucination_chars": removed_chars,
        "segment_count": len(result),
        "timestamped": True,
        "asr_context": context.to_metadata(),
        "adaptive_refinement": refinement_meta,
    }
    if context.initial_prompt:
        meta["initial_prompt"] = context.initial_prompt
    if context.hotwords:
        meta["hotwords"] = context.hotwords
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
                   beam_size=None, quality="balanced", language="en",
                   asr_context=None, adaptive_refinement=True):
    """兼容旧调用方：返回纯文本，但内部使用统一的结构化 ASR。"""
    result = transcribe_mp3_timestamped(
        mp3_path, model_size=model_size, initial_prompt=initial_prompt,
        hotwords=hotwords, beam_size=beam_size, quality=quality,
        language=language, asr_context=asr_context,
        adaptive_refinement=adaptive_refinement)
    return render_segments(result["segments"])


def transcribe(mp3_path, engine="whisper", quality="balanced", asr_model=None,
               initial_prompt=None, hotwords=None, lm_path=None,
               diarize_audio=False, min_speakers=None, max_speakers=None,
               return_metadata=False, language="en", asr_context=None,
               adaptive_refinement=True, align_audio=False):
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
        language=language,
        asr_context=asr_context,
        adaptive_refinement=adaptive_refinement,
    )

    if align_audio and effective_quality in {"balanced", "max"}:
        from asr_alignment import align_segments
        from config import ALIGNMENT_DEVICE, ALIGNMENT_MODE, ALIGNMENT_MODEL

        print("[Align] 执行词级强制对齐...", flush=True)
        aligned = align_segments(
            mp3_path,
            result["segments"],
            language=(
                result["meta"].get("language")
                or language
                or "en"
            ),
            mode=ALIGNMENT_MODE,
            device=ALIGNMENT_DEVICE,
            model_name=ALIGNMENT_MODEL or None,
        )
        result["segments"] = aligned["segments"]
        result["meta"]["alignment"] = aligned["meta"]
        if aligned["meta"].get("warning"):
            result["meta"]["alignment_warning"] = aligned["meta"]["warning"]
        print(
            f"[Align] status={aligned['meta'].get('status')} "
            f"coverage={aligned['meta'].get('word_timestamp_coverage', 0):.1%}",
            flush=True,
        )
    else:
        result["meta"]["alignment"] = {
            "enabled": False,
            "adapter": "whisper_timestamps",
            "status": (
                "fast_quality"
                if align_audio and effective_quality == "fast"
                else "disabled"
            ),
        }

    if diarize_audio:
        from config import require_hf_token
        try:
            require_hf_token()
        except RuntimeError as exc:
            print(
                f"[Diarize][警告] {exc}；本次自动跳过说话人分离",
                flush=True,
            )
            result["meta"]["diarization"] = False
            result["meta"]["diarization_warning"] = "missing_hf_token"
        else:
            from diarize import diarize_and_merge
            diarized = diarize_and_merge(
                mp3_path,
                result["segments"],
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                return_metadata=True,
            )
            result["segments"] = diarized["segments"]
            result["meta"]["diarization"] = True
            result["meta"]["diarization_meta"] = diarized["meta"]
            result["meta"]["diarization_model"] = diarized["meta"].get(
                "model")
            result["meta"]["diarization_exclusive"] = diarized["meta"].get(
                "exclusive_used", False)
            result["meta"]["segment_count"] = len(result["segments"])
            result["meta"]["speaker_count"] = len({
                segment.get("speaker") for segment in result["segments"]
                if segment.get("speaker")
            })
    else:
        result["meta"]["diarization"] = False

    try:
        from transcript_completeness import analyze_audio_completeness
    except ImportError:
        from scripts.transcript_completeness import analyze_audio_completeness
    from config import ASR_COMPLETENESS_MODE
    result["meta"]["completeness_contract_version"] = 1
    result["meta"]["correction_contract_version"] = 1
    result["meta"]["completeness_mode"] = ASR_COMPLETENESS_MODE
    result["meta"]["completeness"] = analyze_audio_completeness(
        mp3_path, result["segments"], mode=ASR_COMPLETENESS_MODE)

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

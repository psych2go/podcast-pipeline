"""Cache bounded source metadata so reviewers can assess URL relevance."""
import hashlib
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

import httpx

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json


SCHEMA_VERSION = 1
CACHE_FILENAME = "source_relevance_cache.json"
MAX_SOURCE_BYTES = 2_000_000
MAX_EXCERPT_CHARS = 2000
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ERROR_RETRY_TTL = timedelta(hours=24)
SUCCESS_REFRESH_TTL = timedelta(days=7)
MAX_CLOCK_SKEW = timedelta(minutes=5)
SOURCE_FETCH_CONCURRENCY = 8
TRACKING_QUERY_KEYS = {
    "cid", "fbclid", "gclid", "mc_cid", "mc_eid", "ref",
}


def normalize_source_url(url):
    """Remove fragments and tracking parameters without changing source identity."""
    value = str(url or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme not in {"http", "https"}:
        return value
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        folded = key.casefold()
        if folded.startswith("utm_") or folded in TRACKING_QUERY_KEYS:
            continue
        query.append((key, item))
    return urlunsplit((
        parts.scheme.casefold(),
        parts.netloc.casefold(),
        parts.path or "/",
        urlencode(query, doseq=True),
        "",
    ))


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = []
        self.text = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._in_title:
            self.title.append(value)
        self.text.append(value)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def expected_source_references(folder):
    folder = Path(folder)
    references = {}
    corrections = _load_json(
        folder / "editorial_corrections.json", {"corrections": []})
    for item in corrections.get("corrections", []) or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("correction_id") or item.get("claim_id")
        for url in item.get("source_urls", []) or []:
            references.setdefault(normalize_source_url(url), set()).add(
                str(reference))
    entities = _load_json(
        folder / "canonical_entities.json", {"entities": []})
    for item in entities.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("entity_id") or item.get("canonical_name")
        for url in item.get("source_urls", []) or []:
            references.setdefault(normalize_source_url(url), set()).add(
                str(reference))
    return {
        url: sorted(filter(None, values))
        for url, values in references.items()
        if urlparse(url).scheme in {"http", "https"}
    }


def expected_source_terms(folder):
    folder = Path(folder)
    terms = {}
    corrections = _load_json(
        folder / "editorial_corrections.json", {"corrections": []})
    for item in corrections.get("corrections", []) or []:
        if not isinstance(item, dict):
            continue
        values = [
            item.get("episode_statement", ""),
            item.get("public_treatment", ""),
        ]
        for url in item.get("source_urls", []) or []:
            terms.setdefault(normalize_source_url(url), set()).update(
                str(value).strip() for value in values if str(value).strip())
    entities = _load_json(
        folder / "canonical_entities.json", {"entities": []})
    for item in entities.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        values = [item.get("canonical_name", "")]
        values.extend(item.get("public_aliases", []) or [])
        for url in item.get("source_urls", []) or []:
            terms.setdefault(normalize_source_url(url), set()).update(
                str(value).strip() for value in values if str(value).strip())
    return {url: sorted(values) for url, values in terms.items()}


def _relevance_tokens(values):
    ignored = {
        "about", "official", "report", "research", "science", "united",
        "states", "公司", "节目", "报告", "研究", "官方",
    }
    tokens = set()
    for value in values or []:
        tokens.update(
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9]{4,}|[一-鿿]{2,}", str(value))
            if token.casefold() not in ignored
        )
    return tokens


def _annotate_relevance(entry, expected_terms):
    expected_tokens = _relevance_tokens(expected_terms)
    observed_tokens = _relevance_tokens([
        entry.get("title", ""), entry.get("excerpt", ""),
    ])
    matched = sorted(expected_tokens & observed_tokens)
    entry["expected_terms"] = list(expected_terms or [])
    entry["matched_terms"] = matched
    entry["relevance_status"] = (
        "matched" if not expected_tokens or matched else "unconfirmed"
    )
    return entry


def _validate_public_source_url(url):
    """Reject source URLs that can address local or non-public networks."""
    value = str(url or "").strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"source URL 无效: {value}") from exc
    if parts.scheme.casefold() not in {"http", "https"}:
        raise ValueError(f"source URL 只允许 HTTP(S): {value}")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError(f"source URL host 或认证信息无效: {value}")
    port = port or (443 if parts.scheme.casefold() == "https" else 80)
    try:
        literal = ipaddress.ip_address(parts.hostname)
        addresses = {literal}
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                parts.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"source URL host 无法解析: {parts.hostname}") from exc
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in resolved
            if item and len(item) > 4 and item[4]
        }
    if not addresses:
        raise ValueError(f"source URL host 没有可用地址: {parts.hostname}")
    blocked = sorted(str(address) for address in addresses if not address.is_global)
    if blocked:
        raise ValueError(
            f"source URL 指向非公网地址: {parts.hostname}: {blocked}")
    return value


def _read_limited_body(response):
    body = bytearray()
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        if len(body) + len(chunk) > MAX_SOURCE_BYTES:
            raise ValueError(
                f"source response 超过 {MAX_SOURCE_BYTES} bytes")
        body.extend(chunk)
    return bytes(body)


def _fetch_source(url):
    current_url = str(url)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _validate_public_source_url(current_url)
            with client.stream("GET", current_url) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        response.raise_for_status()
                    if redirect_count >= MAX_REDIRECTS:
                        raise ValueError("source URL 重定向次数过多")
                    current_url = urljoin(current_url, location)
                    _validate_public_source_url(current_url)
                    continue
                response.raise_for_status()
                body = _read_limited_body(response)
                content_type = response.headers.get("content-type", "")
                encoding = response.charset_encoding or "utf-8"
                final_url = str(response.url)
                status_code = response.status_code
                break
        else:  # pragma: no cover - loop is bounded by explicit redirect handling
            raise ValueError("source URL 重定向次数过多")
    title = ""
    excerpt = ""
    if "html" in content_type.casefold() or body.lstrip().startswith(b"<"):
        parser = _ReadableHTML()
        parser.feed(body.decode(encoding, errors="replace"))
        title = " ".join(parser.title).strip()
        excerpt = " ".join(parser.text).strip()[:MAX_EXCERPT_CHARS]
    else:
        title = Path(urlparse(final_url).path).name or final_url
        if "text" in content_type.casefold() or "json" in content_type.casefold():
            excerpt = body.decode(encoding, errors="replace")
            excerpt = re.sub(r"\s+", " ", excerpt).strip()[:MAX_EXCERPT_CHARS]
    return {
        "status": "fetched",
        "http_status": status_code,
        "final_url": final_url,
        "content_type": content_type,
        "title": title,
        "excerpt": excerpt,
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def _recent_error(entry, now=None):
    if not isinstance(entry, dict) or entry.get("status") != "error":
        return False
    value = entry.get("last_attempt_at") or entry.get("fetched_at")
    try:
        attempted = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    if attempted > now + MAX_CLOCK_SKEW:
        return False
    return now - attempted < ERROR_RETRY_TTL


def _recent_success(entry, now=None):
    if not isinstance(entry, dict) or entry.get("status") != "fetched":
        return False
    value = entry.get("fetched_at") or entry.get("last_attempt_at")
    try:
        fetched = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if fetched > now + MAX_CLOCK_SKEW:
        return False
    return now - fetched < SUCCESS_REFRESH_TTL


def _fetch_cache_entry(url, fetcher, now):
    try:
        entry = fetcher(url)
    except Exception as exc:
        entry = {
            "status": "error",
            "error": str(exc) or type(exc).__name__,
            "error_kind": type(exc).__name__,
            "http_status": getattr(
                getattr(exc, "response", None), "status_code", None),
            "final_url": url,
            "content_type": "",
            "title": "",
            "excerpt": "",
            "content_sha256": "",
        }
    entry["last_attempt_at"] = now.isoformat()
    if entry.get("status") == "fetched":
        entry["fetched_at"] = now.isoformat()
    return entry


def refresh_source_relevance_cache(folder, *, fetcher=None, force=False):
    folder = Path(folder)
    references = expected_source_references(folder)
    relevance_terms = expected_source_terms(folder)
    path = folder / CACHE_FILENAME
    if not references:
        if path.exists():
            payload = {
                "schema_version": SCHEMA_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "entries": {},
            }
            atomic_write_json(path, payload)
            return payload
        return None
    existing = _load_json(path, {"entries": {}})
    old_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
    if isinstance(old_entries, dict):
        old_entries = {
            normalize_source_url(url): entry
            for url, entry in old_entries.items()
        }
    entries = {}
    pending = []
    fetcher = fetcher or _fetch_source
    now = datetime.now(timezone.utc)
    for url, source_ids in references.items():
        previous = old_entries.get(url) if isinstance(old_entries, dict) else None
        reused = (
            not force
            and isinstance(previous, dict)
            and (
                (
                    previous.get("content_sha256")
                    and _recent_success(previous, now)
                )
                or _recent_error(previous, now)
            )
        )
        if reused:
            entries[url] = dict(previous)
        else:
            pending.append(url)
    if pending:
        with ThreadPoolExecutor(
                max_workers=min(SOURCE_FETCH_CONCURRENCY, len(pending))) as pool:
            futures = {
                pool.submit(_fetch_cache_entry, url, fetcher, now): url
                for url in pending
            }
            for future in as_completed(futures):
                entries[futures[future]] = future.result()
    entries = {
        url: _annotate_relevance(
            {
                **entries[url],
                "source_ids": source_ids,
            },
            relevance_terms.get(url, []),
        )
        for url, source_ids in references.items()
    }
    if (
            isinstance(existing, dict)
            and existing.get("schema_version") == SCHEMA_VERSION
            and existing.get("entries") == entries):
        return existing
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    atomic_write_json(path, payload)
    return payload


def validate_source_relevance_cache(payload, expected_references):
    errors = []
    if not isinstance(payload, dict):
        return ["source relevance cache 必须是对象"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"source relevance cache schema_version 必须是 {SCHEMA_VERSION}")
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return errors + ["source relevance cache entries 必须是对象"]
    for url, source_ids in expected_references.items():
        entry = entries.get(url)
        if not isinstance(entry, dict):
            errors.append(f"source relevance cache 缺少 URL: {url}")
            continue
        if entry.get("status") != "fetched":
            errors.append(
                f"source relevance cache 抓取失败: {url}: {entry.get('error', '')}")
        if not entry.get("content_sha256"):
            errors.append(f"source relevance cache 缺少内容哈希: {url}")
        if not entry.get("title") and not entry.get("excerpt"):
            errors.append(f"source relevance cache 缺少标题或摘录: {url}")
        if entry.get("relevance_status") == "unconfirmed":
            errors.append(
                f"source relevance cache 语义相关性未确认: {url}")
        missing_refs = set(source_ids) - set(entry.get("source_ids", []) or [])
        if missing_refs:
            errors.append(
                f"source relevance cache URL 引用不完整: {url}: {sorted(missing_refs)}")
    extra = sorted(set(entries) - set(expected_references))
    if extra:
        errors.append(f"source relevance cache 包含未引用 URL: {extra}")
    return errors

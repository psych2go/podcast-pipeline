"""Cache bounded source metadata so reviewers can assess URL relevance."""
import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json


SCHEMA_VERSION = 1
CACHE_FILENAME = "source_relevance_cache.json"
MAX_SOURCE_BYTES = 2_000_000
MAX_EXCERPT_CHARS = 2000


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
            references.setdefault(str(url), set()).add(str(reference))
    entities = _load_json(
        folder / "canonical_entities.json", {"entities": []})
    for item in entities.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("entity_id") or item.get("canonical_name")
        for url in item.get("source_urls", []) or []:
            references.setdefault(str(url), set()).add(str(reference))
    return {
        url: sorted(filter(None, values))
        for url, values in references.items()
        if urlparse(url).scheme in {"http", "https"}
    }


def _fetch_source(url):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(url)
    response.raise_for_status()
    body = response.content[:MAX_SOURCE_BYTES]
    content_type = response.headers.get("content-type", "")
    title = ""
    excerpt = ""
    if "html" in content_type.casefold() or body.lstrip().startswith(b"<"):
        parser = _ReadableHTML()
        parser.feed(body.decode(response.encoding or "utf-8", errors="replace"))
        title = " ".join(parser.title).strip()
        excerpt = " ".join(parser.text).strip()[:MAX_EXCERPT_CHARS]
    else:
        title = Path(urlparse(str(response.url)).path).name or str(response.url)
        if "text" in content_type.casefold() or "json" in content_type.casefold():
            excerpt = body.decode(response.encoding or "utf-8", errors="replace")
            excerpt = re.sub(r"\s+", " ", excerpt).strip()[:MAX_EXCERPT_CHARS]
    return {
        "status": "fetched",
        "http_status": response.status_code,
        "final_url": str(response.url),
        "content_type": content_type,
        "title": title,
        "excerpt": excerpt,
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def refresh_source_relevance_cache(folder, *, fetcher=None, force=False):
    folder = Path(folder)
    references = expected_source_references(folder)
    path = folder / CACHE_FILENAME
    if not references:
        return None
    existing = _load_json(path, {"entries": {}})
    old_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
    entries = {}
    fetcher = fetcher or _fetch_source
    for url, source_ids in references.items():
        previous = old_entries.get(url) if isinstance(old_entries, dict) else None
        if (
                not force
                and isinstance(previous, dict)
                and previous.get("status") == "fetched"
                and previous.get("content_sha256")):
            entry = dict(previous)
        else:
            try:
                entry = fetcher(url)
            except Exception as exc:
                entry = {
                    "status": "error",
                    "error": str(exc) or type(exc).__name__,
                    "http_status": None,
                    "final_url": url,
                    "content_type": "",
                    "title": "",
                    "excerpt": "",
                    "content_sha256": "",
                }
        entry["source_ids"] = source_ids
        entry["fetched_at"] = datetime.now(timezone.utc).isoformat()
        entries[url] = entry
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
        missing_refs = set(source_ids) - set(entry.get("source_ids", []) or [])
        if missing_refs:
            errors.append(
                f"source relevance cache URL 引用不完整: {url}: {sorted(missing_refs)}")
    extra = sorted(set(entries) - set(expected_references))
    if extra:
        errors.append(f"source relevance cache 包含未引用 URL: {extra}")
    return errors

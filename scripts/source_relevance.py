"""Cache bounded source metadata so reviewers can assess URL relevance."""

import hashlib
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, UTC
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

from scripts.atomic_io import atomic_write_json, atomic_write_text


SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "readable-html-v1"
CACHE_FILENAME = "source_relevance_cache.json"
SHARED_CACHE_ENV = "SOURCE_RELEVANCE_SHARED_CACHE"
SHARED_CACHE_FILENAME = "source_relevance_shared_cache.json"
MAX_SOURCE_BYTES = 2_000_000
MAX_EXCERPT_CHARS = 2000
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ERROR_RETRY_TTL = timedelta(hours=24)
SUCCESS_REFRESH_TTL = timedelta(days=7)
MAX_CLOCK_SKEW = timedelta(minutes=5)
SOURCE_FETCH_CONCURRENCY = 8
TRACKING_QUERY_KEYS = {
    "cid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
}


# The box egress proxy answers DNS for every public host with an address in
# 198.18.0.0/15 (RFC 2544 benchmark range) and forwards the connection. Allow
# that range only when a hostname resolves into it; literal IP URLs in the
# range, and every other private/loopback/link-local range, stay blocked.
SOURCE_USER_AGENT = (
    "Mozilla/5.0 (compatible; podcast-pipeline-source-check/1.0; +https://podcast.zhuying.fun)"
)
_EGRESS_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_egress_proxy_address(address):
    return address.version == 4 and address in _EGRESS_PROXY_NETWORK


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
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


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
    corrections = _load_json(folder / "editorial_corrections.json", {"corrections": []})
    for item in corrections.get("corrections", []) or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("correction_id") or item.get("claim_id")
        for url in item.get("source_urls", []) or []:
            references.setdefault(normalize_source_url(url), set()).add(str(reference))
    entities = _load_json(folder / "canonical_entities.json", {"entities": []})
    for item in entities.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("entity_id") or item.get("canonical_name")
        for url in item.get("source_urls", []) or []:
            references.setdefault(normalize_source_url(url), set()).add(str(reference))
    return {
        url: sorted(filter(None, values))
        for url, values in references.items()
        if urlparse(url).scheme in {"http", "https"}
    }


def expected_source_terms(folder):
    folder = Path(folder)
    terms = {}
    corrections = _load_json(folder / "editorial_corrections.json", {"corrections": []})
    for item in corrections.get("corrections", []) or []:
        if not isinstance(item, dict):
            continue
        values = [
            item.get("episode_statement", ""),
            item.get("public_treatment", ""),
        ]
        for url in item.get("source_urls", []) or []:
            terms.setdefault(normalize_source_url(url), set()).update(
                str(value).strip() for value in values if str(value).strip()
            )
    entities = _load_json(folder / "canonical_entities.json", {"entities": []})
    for item in entities.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        values = [item.get("canonical_name", "")]
        values.extend(item.get("public_aliases", []) or [])
        for url in item.get("source_urls", []) or []:
            terms.setdefault(normalize_source_url(url), set()).update(
                str(value).strip() for value in values if str(value).strip()
            )
    return {url: sorted(values) for url, values in terms.items()}


def _relevance_tokens(values):
    ignored = {
        "about",
        "official",
        "report",
        "research",
        "science",
        "united",
        "states",
        "公司",
        "节目",
        "报告",
        "研究",
        "官方",
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
    observed_tokens = _relevance_tokens(
        [
            entry.get("title", ""),
            entry.get("excerpt", ""),
        ]
    )
    matched = sorted(expected_tokens & observed_tokens)
    entry["expected_terms"] = list(expected_terms or [])
    entry["matched_terms"] = matched
    entry["relevance_status"] = "matched" if not expected_tokens or matched else "unconfirmed"
    return entry


def _resolve_public_source_url(url):
    """Resolve a source URL once and return only approved public addresses."""
    value = str(url or "").strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"source URL 无效: {value}") from exc
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError(f"source URL 只允许 HTTP(S): {value}")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError(f"source URL host 或认证信息无效: {value}")
    port = port or (443 if scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(parts.hostname)
        addresses = [literal]
        literal_host = literal
    except ValueError:
        literal_host = None
        try:
            resolved = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"source URL host 无法解析: {parts.hostname}") from exc
        addresses = []
        for item in resolved:
            if not item or len(item) <= 4 or not item[4]:
                continue
            address = ipaddress.ip_address(item[4][0])
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise ValueError(f"source URL host 没有可用地址: {parts.hostname}")
    blocked = sorted(
        str(address)
        for address in addresses
        if not address.is_global
        and not (literal_host is None and _is_egress_proxy_address(address))
    )
    if blocked:
        raise ValueError(f"source URL 指向非公网地址: {parts.hostname}: {blocked}")
    addresses.sort(key=lambda address: (address.version != 4, str(address)))
    return value, parts, addresses


def _validate_public_source_url(url):
    """Reject source URLs that can address local or non-public networks."""
    value, _parts, _addresses = _resolve_public_source_url(url)
    return value


def _pinned_source_request(parts, address):
    scheme = parts.scheme.casefold()
    port = parts.port or (443 if scheme == "https" else 80)
    address_text = str(address)
    address_host = f"[{address_text}]" if address.version == 6 else address_text
    pinned_url = urlunsplit(
        (
            scheme,
            f"{address_host}:{port}",
            parts.path or "/",
            parts.query,
            "",
        )
    )
    default_port = 443 if scheme == "https" else 80
    original_host = parts.hostname
    host_value = f"[{original_host}]" if ":" in original_host else original_host
    if port != default_port:
        host_value = f"{host_value}:{port}"
    return pinned_url, host_value, original_host


def _read_limited_body(response):
    body = bytearray()
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        if len(body) + len(chunk) > MAX_SOURCE_BYTES:
            raise ValueError(f"source response 超过 {MAX_SOURCE_BYTES} bytes")
        body.extend(chunk)
    return bytes(body)


def _fetch_source(url):
    current_url = str(url)
    for redirect_count in range(MAX_REDIRECTS + 1):
        current_url, parts, addresses = _resolve_public_source_url(current_url)
        pinned_url, host_header, sni_hostname = _pinned_source_request(parts, addresses[0])
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                "GET",
                pinned_url,
                headers={"Host": host_header, "User-Agent": SOURCE_USER_AGENT},
                extensions={"sni_hostname": sni_hostname},
            ) as response:
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
                final_url = current_url
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
    now = now or datetime.now(UTC)
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=UTC)
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
    now = now or datetime.now(UTC)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
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
            "http_status": getattr(getattr(exc, "response", None), "status_code", None),
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


def _shared_cache_path():
    """Return the process-wide cache path (never inside published content)."""
    import os

    configured = os.environ.get(SHARED_CACHE_ENV, "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "podcast-pipeline" / SHARED_CACHE_FILENAME
    )


def _load_shared_entries():
    payload = _load_json(_shared_cache_path(), {})
    return payload.get("entries", {}) if isinstance(payload, dict) else {}


def _store_shared_entries(entries):
    path = _shared_cache_path()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def refresh_source_relevance_cache(folder, *, fetcher=None, force=False):
    folder = Path(folder)
    references = expected_source_references(folder)
    relevance_terms = expected_source_terms(folder)
    path = folder / CACHE_FILENAME
    if not references:
        if path.exists():
            existing = _load_json(path, {})
            if existing.get("schema_version") == SCHEMA_VERSION and existing.get("entries") == {}:
                return existing
            payload = {
                "schema_version": SCHEMA_VERSION,
                "updated_at": datetime.now(UTC).isoformat(),
                "entries": {},
            }
            atomic_write_json(path, payload)
            return payload
        return None
    existing = _load_json(path, {"entries": {}})
    old_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
    shared_entries = _load_shared_entries()
    if isinstance(old_entries, dict):
        old_entries = {normalize_source_url(url): entry for url, entry in old_entries.items()}
    entries = {}
    pending = []
    fetcher = fetcher or _fetch_source
    now = datetime.now(UTC)
    for url, _source_ids in references.items():
        previous = old_entries.get(url) if isinstance(old_entries, dict) else None
        shared = shared_entries.get(url) if isinstance(shared_entries, dict) else None
        if (
            not force
            and isinstance(shared, dict)
            and shared.get("status") == "fetched"
            and _recent_success(shared, now)
        ):
            previous = shared
        reused = (
            not force
            and isinstance(previous, dict)
            and (
                (previous.get("content_sha256") and _recent_success(previous, now))
                or _recent_error(previous, now)
            )
        )
        if reused:
            entries[url] = dict(previous)
        else:
            pending.append(url)
    if pending:
        with ThreadPoolExecutor(max_workers=min(SOURCE_FETCH_CONCURRENCY, len(pending))) as pool:
            futures = {pool.submit(_fetch_cache_entry, url, fetcher, now): url for url in pending}
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
    successful = {
        url: dict(entry)
        for url, entry in entries.items()
        if entry.get("status") == "fetched" and entry.get("content_sha256")
    }
    if successful:
        _store_shared_entries({**shared_entries, **successful})
    if (
        isinstance(existing, dict)
        and existing.get("schema_version") == SCHEMA_VERSION
        and existing.get("entries") == entries
    ):
        return existing
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": entries,
    }
    atomic_write_json(path, payload)
    return payload


def _unconfirmed_source_urls(payload, expected_references):
    """Return citations that still cannot be confirmed after a refresh."""
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return set(expected_references)
    bad = set()
    for url in expected_references:
        entry = entries.get(url)
        if not isinstance(entry, dict):
            bad.add(url)
            continue
        if (
            entry.get("status") != "fetched"
            or not entry.get("content_sha256")
            or not entry.get("title") and not entry.get("excerpt")
            or entry.get("relevance_status") == "unconfirmed"
        ):
            bad.add(url)
    return bad


def _drop_source_urls(value, removed):
    """Remove only citation fields; URLs identifying the episode stay intact."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "source_urls" and isinstance(item, list):
                result[key] = [
                    url for url in item
                    if normalize_source_url(url) not in removed
                ]
            else:
                result[key] = _drop_source_urls(item, removed)
        return result
    if isinstance(value, list):
        return [_drop_source_urls(item, removed) for item in value]
    return value


def _strip_public_citation_links(text, removed):
    """Remove failed citation links while retaining readable link labels."""
    def replace(match):
        url = normalize_source_url(match.group("url"))
        return match.group("label") if url in removed else match.group(0)

    pattern = re.compile(
        r"\[(?P<label>[^\]]+)\]\(<?(?P<url>https?://[^\s)>]+)>?\)"
    )
    updated = pattern.sub(replace, text)
    for url in removed:
        updated = updated.replace(url, "")
    return updated


def _restamp_content_bindings(folder):
    """Re-run canonical finalization and refresh hashes after citation edits."""
    folder = Path(folder)
    briefing_path = folder / "讲书稿.md"
    summary_path = folder / "summary_map.json"
    if briefing_path.exists() and summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            from scripts.content_finalizer import finalize_content_artifacts

            briefing, summary, _changes = finalize_content_artifacts(
                briefing_path.read_text(encoding="utf-8"), summary
            )
            atomic_write_text(briefing_path, briefing)
            try:
                from scripts.agent_pipeline import _writing_input_hashes

                summary["writing_inputs_version"] = 1
                summary["writing_inputs"] = _writing_input_hashes(folder)
            except (ImportError, OSError, ValueError, TypeError):
                pass
            atomic_write_json(summary_path, summary)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError):
            # The quality gate remains authoritative for malformed content; a
            # citation repair must not hide that independent validation error.
            pass


def repair_unconfirmed_source_citations(folder, payload=None):
    """Drop citations whose refreshed cache entry remains unconfirmed.

    This is deliberately conservative: it edits citation ledgers and public
    markdown links, never transcript evidence or the episode's source URL.
    Entity records that would otherwise violate the required-source contract
    are removed, and all generated bindings are re-stamped before validation.
    """
    folder = Path(folder)
    references = expected_source_references(folder)
    payload = payload if isinstance(payload, dict) else _load_json(
        folder / CACHE_FILENAME, {"entries": {}}
    )
    removed = _unconfirmed_source_urls(payload, references)
    if not removed:
        return {"removed_urls": [], "changed_files": [], "restamped": False}

    changed_files = []
    json_paths = [
        folder / "canonical_entities.json",
        folder / "editorial_fact_checks.json",
        *(
            sorted((folder / "editorial_fact_check_batches").glob("*.json"))
            if (folder / "editorial_fact_check_batches").is_dir()
            else []
        ),
    ]
    for path in json_paths:
        if not path.exists():
            continue
        try:
            original = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        updated = _drop_source_urls(original, removed)
        if path.name == "canonical_entities.json" and isinstance(updated, dict):
            identity_types = {"person", "company", "product", "institution", "title"}
            updated["entities"] = [
                entity for entity in updated.get("entities", []) or []
                if not isinstance(entity, dict)
                or entity.get("entity_type") not in identity_types
                or entity.get("source_urls")
            ]
        if path.name == "editorial_fact_checks.json" and isinstance(updated, dict):
            for claim in updated.get("claims", []) or []:
                if not isinstance(claim, dict):
                    continue
                checks = claim.get("checks", []) or []
                if not any(check.get("source_urls") for check in checks if isinstance(check, dict)):
                    claim["requires_web"] = False
                for check in checks:
                    if isinstance(check, dict) and not check.get("source_urls"):
                        check["editorial_correction"] = ""
            for issue in updated.get("issue_inventory", []) or []:
                if isinstance(issue, dict) and not issue.get("source_urls"):
                    issue["source_urls"] = []
        if updated != original:
            atomic_write_json(path, updated)
            changed_files.append(path.relative_to(folder).as_posix())

    for name in ("中文完整笔记.md", "讲书稿.md"):
        path = folder / name
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        updated = _strip_public_citation_links(original, removed)
        if updated != original:
            atomic_write_text(path, updated)
            changed_files.append(name)

    _restamp_content_bindings(folder)
    raw_entries = payload.get("entries", {})
    if not isinstance(raw_entries, dict):
        raw_entries = {}
    cache_entries = {
        url: entry for url, entry in raw_entries.items()
        if url in expected_source_references(folder)
    }
    cache_payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": cache_entries,
    }
    atomic_write_json(folder / CACHE_FILENAME, cache_payload)
    for name in ("ai_review.json", "quality_report.json"):
        path = folder / name
        if path.exists():
            path.unlink()
            changed_files.append(name)
    try:
        from scripts.episode import sync_episode_state

        sync_episode_state(folder)
    except (OSError, ValueError, TypeError):
        pass
    try:
        from scripts.artifact_manifest import write_artifact_manifest

        write_artifact_manifest(folder)
    except (OSError, ValueError, TypeError):
        pass
    return {
        "removed_urls": sorted(removed),
        "changed_files": sorted(set(changed_files + [CACHE_FILENAME])),
        "restamped": True,
    }


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
            errors.append(f"source relevance cache 抓取失败: {url}: {entry.get('error', '')}")
        if not entry.get("content_sha256"):
            errors.append(f"source relevance cache 缺少内容哈希: {url}")
        if not entry.get("title") and not entry.get("excerpt"):
            errors.append(f"source relevance cache 缺少标题或摘录: {url}")
        if entry.get("relevance_status") == "unconfirmed":
            errors.append(f"source relevance cache 语义相关性未确认: {url}")
        missing_refs = set(source_ids) - set(entry.get("source_ids", []) or [])
        if missing_refs:
            errors.append(f"source relevance cache URL 引用不完整: {url}: {sorted(missing_refs)}")
    extra = sorted(set(entries) - set(expected_references))
    if extra:
        errors.append(f"source relevance cache 包含未引用 URL: {extra}")
    return errors

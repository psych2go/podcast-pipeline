"""Canonical entity contract for names used in public podcast content."""
import re
from urllib.parse import urlparse


SCHEMA_VERSION = 1
ENTITY_TYPES = (
    "person", "company", "product", "institution", "title",
    "place", "technical_term", "other",
)
CONFIDENCE_VALUES = ("high", "medium")

GENERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string", "pattern": r"^EN\d{4,}$",
                    },
                    "canonical_name": {"type": "string", "minLength": 1},
                    "observed_names": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "public_aliases": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "entity_type": {
                        "type": "string", "enum": list(ENTITY_TYPES),
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "segment_ids": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string", "pattern": r"^S\d{4,}$"},
                    },
                    "confidence": {
                        "type": "string", "enum": list(CONFIDENCE_VALUES),
                    },
                    "rationale": {"type": "string", "minLength": 10},
                },
            },
        },
    },
}


def _http_url(value):
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_canonical_entities(payload, transcript=None):
    errors = []
    if not isinstance(payload, dict):
        return ["canonical_entities 必须是对象"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"canonical_entities.schema_version 必须是 {SCHEMA_VERSION}")
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return errors + ["canonical_entities.entities 必须是数组"]
    transcript_ids = {
        segment.get("id")
        for segment in (transcript or {}).get("segments", [])
        if isinstance(segment, dict) and segment.get("id")
    }
    seen_ids = set()
    seen_names = {}
    for index, item in enumerate(entities):
        prefix = f"entities[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        entity_id = item.get("entity_id")
        if not re.fullmatch(r"EN\d{4,}", str(entity_id or "")):
            errors.append(f"{prefix}.entity_id 无效: {entity_id!r}")
        elif entity_id in seen_ids:
            errors.append(f"重复 entity_id: {entity_id}")
        seen_ids.add(entity_id)
        canonical = str(item.get("canonical_name", "")).strip()
        if not canonical:
            errors.append(f"{prefix}.canonical_name 不能为空")
        observed = item.get("observed_names")
        if not isinstance(observed, list) or not observed:
            errors.append(f"{prefix}.observed_names 不能为空")
            observed = []
        for name in [canonical, *observed]:
            normalized = str(name).strip().casefold()
            if not normalized:
                continue
            previous = seen_names.get(normalized)
            if previous and previous != canonical:
                errors.append(
                    f"实体别名冲突: {name!r} 同时指向 {previous!r} 和 {canonical!r}")
            seen_names[normalized] = canonical
        public_aliases = item.get("public_aliases")
        if not isinstance(public_aliases, list):
            errors.append(f"{prefix}.public_aliases 必须是数组")
            public_aliases = []
        for alias in public_aliases:
            alias = str(alias).strip()
            if not alias:
                errors.append(f"{prefix}.public_aliases 不得包含空值")
                continue
            normalized = alias.casefold()
            previous = seen_names.get(normalized)
            if previous and previous != canonical:
                errors.append(
                    f"实体公开别名冲突: {alias!r} 同时指向 "
                    f"{previous!r} 和 {canonical!r}")
            seen_names[normalized] = canonical
        if item.get("entity_type") not in ENTITY_TYPES:
            errors.append(f"{prefix}.entity_type 无效")
        if item.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"{prefix}.confidence 无效")
        if len(re.sub(r"\s+", "", str(item.get("rationale", "")))) < 10:
            errors.append(f"{prefix}.rationale 过短")
        segment_ids = item.get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            errors.append(f"{prefix}.segment_ids 不能为空")
        elif transcript is not None:
            unknown = sorted(set(segment_ids) - transcript_ids)
            if unknown:
                errors.append(f"{prefix}.segment_ids 包含未知片段: {unknown}")
        urls = item.get("source_urls")
        if not isinstance(urls, list):
            errors.append(f"{prefix}.source_urls 必须是数组")
            urls = []
        invalid_urls = [url for url in urls if not _http_url(url)]
        if invalid_urls:
            errors.append(f"{prefix}.source_urls 包含无效 URL: {invalid_urls}")
        if (
                item.get("entity_type") in {
                    "person", "company", "product", "institution", "title"}
                and not urls):
            errors.append(f"{prefix}: 公开实体必须提供规范来源 URL")
    return errors


def public_entity_alias_errors(payload, *texts):
    """Reject non-canonical observed aliases that leaked into public prose."""
    errors = []
    joined = "\n".join(text or "" for text in texts)
    for item in payload.get("entities", []) or []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical_name", "")).strip()
        public_aliases = [
            str(value).strip()
            for value in item.get("public_aliases", []) or []
            if str(value).strip()
        ]
        allowed = {value.casefold() for value in public_aliases}
        allowed_spans = []
        for allowed_name in [canonical, *public_aliases]:
            if not allowed_name:
                continue
            flags = (
                0 if allowed_name.casefold() == canonical.casefold()
                else re.IGNORECASE
            )
            allowed_pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(allowed_name)}(?![A-Za-z0-9])",
                flags,
            )
            allowed_spans.extend(
                (match.start(), match.end())
                for match in allowed_pattern.finditer(joined)
            )
        for observed in item.get("observed_names", []) or []:
            observed = str(observed).strip()
            if (
                    not observed
                    or observed == canonical
                    or observed.casefold() in allowed):
                continue
            flags = (
                0 if observed.casefold() == canonical.casefold()
                else re.IGNORECASE
            )
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(observed)}(?![A-Za-z0-9])",
                flags,
            )
            leaked = any(
                not any(
                    start <= match.start() and match.end() <= end
                    for start, end in allowed_spans
                )
                for match in pattern.finditer(joined)
            )
            if leaked:
                errors.append(
                    f"公开文本仍包含非规范实体名 {observed!r}，应使用 {canonical!r}")
    return errors

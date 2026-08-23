"""Validated ledger for external corrections to transcript-backed claims."""
import json
from pathlib import Path
from urllib.parse import urlparse


SCHEMA_VERSION = 1
VERDICTS = {"corrected", "qualified", "uncertain", "excluded"}
RISK_DOMAINS = {"general", "medical", "legal", "financial", "political", "safety"}


def load_editorial_corrections(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "corrections": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _http_url(value):
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_editorial_corrections(payload, valid_claim_ids=None):
    """Validate corrections without treating them as transcript evidence."""
    errors = []
    if not isinstance(payload, dict):
        return ["editorial_corrections 必须是对象"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"editorial_corrections.schema_version 必须是 {SCHEMA_VERSION}")
    corrections = payload.get("corrections")
    if not isinstance(corrections, list):
        return errors + ["editorial_corrections.corrections 必须是数组"]
    seen = set()
    for index, item in enumerate(corrections):
        prefix = f"corrections[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        correction_id = item.get("correction_id")
        if not isinstance(correction_id, str) or not correction_id.strip():
            errors.append(f"{prefix}.correction_id 不能为空")
        elif correction_id in seen:
            errors.append(f"重复 correction_id: {correction_id}")
        seen.add(correction_id)
        claim_id = item.get("claim_id")
        if valid_claim_ids is not None and claim_id not in valid_claim_ids:
            errors.append(f"{prefix}.claim_id 不存在于 content_map: {claim_id}")
        if not str(item.get("episode_statement", "")).strip():
            errors.append(f"{prefix}.episode_statement 不能为空")
        if not str(item.get("public_treatment", "")).strip():
            errors.append(f"{prefix}.public_treatment 不能为空")
        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            errors.append(f"{prefix}.verdict 无效: {verdict!r}")
        if item.get("risk_domain") not in RISK_DOMAINS:
            errors.append(f"{prefix}.risk_domain 无效")
        if not str(item.get("checked_at", "")).strip():
            errors.append(f"{prefix}.checked_at 不能为空")
        urls = item.get("source_urls")
        if not isinstance(urls, list):
            errors.append(f"{prefix}.source_urls 必须是数组")
            urls = []
        invalid_urls = [url for url in urls if not _http_url(url)]
        if invalid_urls:
            errors.append(f"{prefix}.source_urls 包含无效 URL: {invalid_urls}")
        if verdict in {"corrected", "qualified"} and not urls:
            errors.append(f"{prefix}: corrected/qualified 必须提供公开来源")
    return errors

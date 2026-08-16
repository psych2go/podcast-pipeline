"""Auditable local cache for web-backed AI fact checks."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from claim_taxonomy import is_cacheable_fact_check
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.claim_taxonomy import is_cacheable_fact_check


CACHE_FILENAME = "fact_check_cache.json"
CACHE_SCHEMA_VERSION = 3
DEFAULT_DYNAMIC_TTL_DAYS = 7


def normalize_claim(claim):
    return re.sub(r"\s+", " ", str(claim or "")).strip().casefold()


def claim_sha256(claim):
    return hashlib.sha256(normalize_claim(claim).encode("utf-8")).hexdigest()


def cache_key(claim, source_url, source_date=""):
    payload = "\n".join((
        claim_sha256(claim),
        str(source_url or "").strip(),
        str(source_date or "").strip(),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_cache(folder):
    path = Path(folder) / CACHE_FILENAME
    if not path.exists():
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    return payload


def get_cached_fact_check(
        folder, claim, source_url, *, dynamic=False,
        ttl_days=DEFAULT_DYNAMIC_TTL_DAYS, now=None):
    """Return the newest matching entry unless a dynamic fact has expired."""
    now = now or datetime.now(timezone.utc)
    claim_hash = claim_sha256(claim)
    matches = [
        entry for entry in load_cache(folder).get("entries", {}).values()
        if isinstance(entry, dict)
        and entry.get("claim_sha256") == claim_hash
        and entry.get("source_url") == str(source_url or "").strip()
    ]
    matches.sort(key=lambda item: str(item.get("checked_at", "")), reverse=True)
    for entry in matches:
        if dynamic or entry.get("dynamic"):
            checked_at = _parse_time(entry.get("checked_at"))
            if checked_at is None or now - checked_at > timedelta(days=ttl_days):
                continue
        return entry
    return None


def store_fact_check(
        folder, fact_check, source_url, *, dynamic=False, source_date=""):
    folder = Path(folder)
    payload = load_cache(folder)
    claim = str(fact_check.get("claim", "")).strip()
    checked_at = str(fact_check.get("checked_at", "")).strip()
    if (
            not claim or not source_url or not checked_at
            or not is_cacheable_fact_check(fact_check)):
        return None
    source_date = str(source_date or checked_at[:10])
    key = cache_key(claim, source_url, source_date)
    entry = {
        "key": key,
        "claim": claim,
        "normalized_claim": normalize_claim(claim),
        "claim_sha256": claim_sha256(claim),
        "parent_claim_id": fact_check.get("parent_claim_id"),
        "subclaim_id": fact_check.get("subclaim_id"),
        "claim_type": fact_check.get("claim_type"),
        "claim_origin": fact_check.get("claim_origin"),
        "speaker_role": fact_check.get("speaker_role"),
        "assertion_type": fact_check.get("assertion_type"),
        "verification_mode": fact_check.get("verification_mode"),
        "risk_domain": fact_check.get("risk_domain"),
        "source_url": str(source_url).strip(),
        "source_date": source_date,
        "checked_at": checked_at,
        "dynamic": bool(dynamic),
        "verdict": fact_check.get("verdict"),
        "publication_status": fact_check.get("publication_status"),
        "notes": fact_check.get("notes", ""),
    }
    payload.setdefault("entries", {})[key] = entry
    payload["schema_version"] = CACHE_SCHEMA_VERSION
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(folder / CACHE_FILENAME, payload)
    return entry



def build_cache_context(
        folder, claims, *, ttl_days=DEFAULT_DYNAMIC_TTL_DAYS, now=None):
    """Return fresh exact-claim cache leads for an independent reviewer.

    The result is deliberately non-authoritative: callers must still match the
    current source URL and independently decide the verdict.
    """
    now = now or datetime.now(timezone.utc)
    wanted = {claim_sha256(claim) for claim in claims if str(claim).strip()}
    matches = []
    expired = 0
    for entry in load_cache(folder).get("entries", {}).values():
        if not isinstance(entry, dict) or entry.get("claim_sha256") not in wanted:
            continue
        if entry.get("dynamic"):
            checked_at = _parse_time(entry.get("checked_at"))
            if checked_at is None or now - checked_at > timedelta(days=ttl_days):
                expired += 1
                continue
        matches.append({
            key: entry.get(key)
            for key in (
                "claim", "claim_sha256", "parent_claim_id", "subclaim_id",
                "claim_type", "assertion_type", "verification_mode",
                "risk_domain", "source_url", "source_date", "checked_at",
                "dynamic", "verdict", "publication_status", "notes",
            )
        })
    matches.sort(key=lambda item: (
        str(item.get("claim_sha256", "")),
        str(item.get("source_url", "")),
        str(item.get("checked_at", "")),
    ))
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "authoritative": False,
        "matching_rule": "exact normalized claim hash; source URL must also match",
        "ttl_days": ttl_days,
        "matched_entries": matches,
        "expired_dynamic_entries": expired,
    }

def _looks_dynamic(claim):
    text = str(claim or "").casefold()
    markers = (
        "目前", "当前", "截至", "最新", "如今", "现在", "今年", "本月",
        "today", "current", "latest", "now", "market cap", "valuation",
        "price", "revenue", "employees", "用户数", "估值", "市值", "价格",
    )
    return any(marker in text for marker in markers)


def update_cache_from_review(folder, review):
    stored = 0
    for fact_check in review.get("fact_checks", []) or []:
        if not isinstance(fact_check, dict):
            continue
        if not is_cacheable_fact_check(fact_check):
            continue
        dynamic = _looks_dynamic(fact_check.get("claim"))
        for source_url in fact_check.get("source_urls", []) or []:
            if store_fact_check(
                    folder, fact_check, source_url, dynamic=dynamic):
                stored += 1
    return stored

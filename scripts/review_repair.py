"""Bounded review/repair loop that never weakens review thresholds."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from ai_review import review_episode
    from atomic_io import atomic_write_json, atomic_write_text
    from claim_evidence import refine_claim_evidence
    from content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        load_json,
        save_json,
    )
    from content_finalizer import (
        finalize_content_package,
        generate_safe_tts_lexicon,
        validate_tts_readiness,
    )
    from tts import load_tts_lexicon
    from hashing import sha256_file
    from prewrite_fact_checks import (
        FILENAME as PREWRITE_FACT_CHECKS_FILENAME,
        validate_ledger as validate_prewrite_fact_checks,
    )
except ImportError:
    from scripts.ai_review import review_episode
    from scripts.atomic_io import atomic_write_json, atomic_write_text
    from scripts.claim_evidence import refine_claim_evidence
    from scripts.content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        load_json,
        save_json,
    )
    from scripts.content_finalizer import (
        finalize_content_package,
        generate_safe_tts_lexicon,
        validate_tts_readiness,
    )
    from scripts.tts import load_tts_lexicon
    from scripts.hashing import sha256_file
    from scripts.prewrite_fact_checks import (
        FILENAME as PREWRITE_FACT_CHECKS_FILENAME,
        validate_ledger as validate_prewrite_fact_checks,
    )


SAFE_SUMMARY_CATEGORIES = {"summary_map", "summary", "coverage_mapping"}
SAFE_TTS_CATEGORIES = {"tts", "tts_lexicon", "pronunciation"}
SAFE_EVIDENCE_CATEGORIES = {"evidence_integrity", "claim_evidence"}
BLOCKED_CATEGORIES = {
    "factuality", "medical", "health", "attribution", "numbers",
    "transcript_quality", "entity_accuracy",
}
SAFE_EXACT_ENTITY_CATEGORIES = {
    "entity_accuracy", "transcript_quality", "transcript_title_normalization",
}
ENTITY_REPAIR_FILES = {
    "转录_纠错.txt", "content_map.json", "中文完整笔记.md",
    "讲书稿.md", "tts_lexicon.json",
}


def _is_http_url(value):
    return bool(re.match(r"^https?://[^\s]+$", str(value or "").strip()))


def _is_exact_entity_issue(issue):
    old = str(issue.get("replacement_from", "")).strip()
    new = str(issue.get("replacement_to", "")).strip()
    files = issue.get("allowed_files") or []
    return (
        issue.get("repair_kind") == "exact_entity"
        and _category(issue) in SAFE_EXACT_ENTITY_CATEGORIES
        and 3 <= len(old) <= 200
        and 1 <= len(new) <= 200
        and old != new
        and "\n" not in old
        and "\n" not in new
        and isinstance(files, list)
        and bool(files)
        and set(files) <= ENTITY_REPAIR_FILES
        and bool(issue.get("source_urls"))
        and all(_is_http_url(url) for url in issue.get("source_urls", []))
    )


def _replace_value(value, old, new):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_value(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_value(item, old, new) for key, item in value.items()}
    return value


def _protected_content_map_snapshot(payload):
    return [
        {
            "id": unit.get("id"),
            "source_excerpt": unit.get("source_excerpt"),
            "evidence": unit.get("evidence"),
            "timestamps": unit.get("timestamps"),
            "claim_evidence": unit.get("claim_evidence"),
            "claim_evidence_sha256": unit.get("claim_evidence_sha256"),
        }
        for unit in payload.get("units", []) or []
        if isinstance(unit, dict)
    ]


def _replace_content_map_entity(path, old, new):
    payload = load_json(path)
    protected = _protected_content_map_snapshot(payload)
    changed = 0
    if isinstance(payload.get("title"), str):
        replaced = payload["title"].replace(old, new)
        changed += replaced != payload["title"]
        payload["title"] = replaced
    semantic_keys = (
        "topic", "speaker", "claims", "reasoning", "examples", "numbers",
        "terms", "notes", "claim_evidence_notes",
    )
    for unit in payload.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        for key in semantic_keys:
            if key not in unit:
                continue
            before = json.dumps(unit[key], ensure_ascii=False, sort_keys=True)
            unit[key] = _replace_value(unit[key], old, new)
            after = json.dumps(unit[key], ensure_ascii=False, sort_keys=True)
            changed += before != after
    if _protected_content_map_snapshot(payload) != protected:
        raise RuntimeError("精确实体修复试图修改 content_map 原始证据字段")
    if changed:
        save_json(path, payload)
    return int(changed)


def _replace_tts_lexicon_entity(path, old, new):
    payload = load_json(path)
    updated = {}
    changed = 0
    for key, value in payload.items():
        replacement_key = str(key).replace(old, new)
        if replacement_key in updated and replacement_key != key:
            raise RuntimeError("精确实体修复导致 TTS 词典 key 冲突")
        updated[replacement_key] = value
        changed += replacement_key != key
    if changed:
        save_json(path, updated)
    return int(changed)


def _replace_text_entity(path, old, new):
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count:
        atomic_write_text(path, text.replace(old, new))
    return count


def _transcript_basis(folder):
    corrected = folder / "转录_纠错.txt"
    path = corrected if corrected.exists() else folder / "原始转录.txt"
    return {
        "file": path.name,
        "sha256": body_sha256(path.read_text(encoding="utf-8")),
    }


def _replace_ledger_entity(value, old, new, *, key=""):
    protected_keys = {
        "source_urls", "content_map_sha256", "sha256", "parent_claim_id",
        "subclaim_id", "evidence_segment_ids",
    }
    if key in protected_keys:
        return value
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [
            _replace_ledger_entity(item, old, new, key=key)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            child_key: _replace_ledger_entity(
                item, old, new, key=child_key)
            for child_key, item in value.items()
        }
    return value


def _refresh_ledger_binding(folder, replacements):
    path = folder / PREWRITE_FACT_CHECKS_FILENAME
    if not path.exists():
        return
    payload = load_json(path)
    for old, new in replacements:
        payload = _replace_ledger_entity(payload, old, new)
    payload["content_map_sha256"] = sha256_file(folder / "content_map.json")
    payload["transcript_basis"] = _transcript_basis(folder)
    atomic_write_json(path, payload)
    errors = validate_prewrite_fact_checks(folder, payload)
    if errors:
        raise RuntimeError(
            "精确实体修复后预写作台账失效: " + "; ".join(errors[:5]))


def _refresh_semantic_bindings(folder, replacements):
    raw_path = folder / "transcript.raw.json"
    content_map_path = folder / "content_map.json"
    summary_path = folder / "summary_map.json"
    notes_path = folder / "中文完整笔记.md"
    transcript = load_json(raw_path)
    content_map = load_json(content_map_path)
    content_map, _transcript = enrich_content_map_evidence(
        content_map, transcript)
    save_json(content_map_path, content_map)
    finalized = finalize_content_package(folder)
    briefing = finalized["briefing"]
    summary = finalized["summary_map"]
    notes = notes_path.read_text(encoding="utf-8")
    summary = enrich_summary_map_evidence(
        summary, notes, content_map, briefing)
    summary["transcript_basis"] = _transcript_basis(folder)
    save_json(summary_path, summary)
    _refresh_ledger_binding(folder, replacements)


def _repair_exact_entities(folder, issues):
    folder = Path(folder)
    raw_before = (folder / "原始转录.txt").read_bytes()
    transcript_before = (folder / "transcript.raw.json").read_bytes()
    replacements = []
    changes = []
    for issue in issues:
        old = str(issue["replacement_from"]).strip()
        new = str(issue["replacement_to"]).strip()
        changed_files = {}
        for name in issue.get("allowed_files", []):
            path = folder / name
            if not path.exists():
                continue
            if name == "content_map.json":
                count = _replace_content_map_entity(path, old, new)
            elif name == "tts_lexicon.json":
                count = _replace_tts_lexicon_entity(path, old, new)
            else:
                count = _replace_text_entity(path, old, new)
            if count:
                changed_files[name] = count
        if not changed_files:
            raise RuntimeError(f"精确实体修复未找到目标文本: {old!r}")
        replacements.append((old, new))
        changes.append({
            "from": old,
            "to": new,
            "files": changed_files,
            "source_urls": issue.get("source_urls", []),
        })
    if (folder / "原始转录.txt").read_bytes() != raw_before:
        raise RuntimeError("精确实体修复不得修改 原始转录.txt")
    if (folder / "transcript.raw.json").read_bytes() != transcript_before:
        raise RuntimeError("精确实体修复不得修改 transcript.raw.json")
    _refresh_semantic_bindings(folder, replacements)
    return {
        "action": "evidence_backed_exact_entity_repair",
        "changes": changes,
    }


def _category(issue):
    return str(issue.get("category", "")).strip().casefold()


def _issue_text(issue):
    return " ".join(str(issue.get(key, "")) for key in (
        "statement", "source_evidence", "recommendation", "file"))


def _repair_summary(folder):
    result = finalize_content_package(folder)
    return {
        "action": "finalize_content_package",
        "normalization_changes": result.get("normalization_changes", []),
    }


def _repair_tts(folder):
    folder = Path(folder)
    briefing = (folder / "讲书稿.md").read_text(encoding="utf-8")
    existing = load_tts_lexicon(folder)
    generated = generate_safe_tts_lexicon(briefing, existing)
    if generated != existing:
        atomic_write_json(folder / "tts_lexicon.json", generated)
    unresolved = validate_tts_readiness(briefing, generated)
    if unresolved:
        raise RuntimeError(
            "安全 TTS 自动修复后仍有未确认读音: " + "; ".join(unresolved))
    return {
        "action": "refresh_safe_tts_lexicon",
        "entries": len(generated),
    }


def _repair_evidence(folder, issues):
    unit_ids = sorted(set(re.findall(
        r"\bU\d{4}\b", " ".join(_issue_text(issue) for issue in issues))))
    if not unit_ids:
        raise RuntimeError("evidence_integrity issue 未明确指出 unit ID，禁止猜测重做范围")
    metrics = refine_claim_evidence(folder, unit_ids=unit_ids)
    return {
        "action": "refine_claim_evidence",
        "unit_ids": unit_ids,
        "claim_count": metrics.get("claim_count", 0),
    }


def repair_safe_issues(folder, review):
    """Apply deterministic repairs only; return actions and blocking reasons."""
    issues = [
        issue for issue in review.get("issues", []) or []
        if isinstance(issue, dict)
    ]
    high_issues = [
        issue for issue in issues
        if issue.get("severity") in {"critical", "high"}
    ]
    exact_entity_issues = [
        issue for issue in high_issues if _is_exact_entity_issue(issue)
    ]
    blocked = [
        issue for issue in high_issues
        if _category(issue) in BLOCKED_CATEGORIES
        and issue not in exact_entity_issues
    ]
    unknown = [
        issue for issue in high_issues
        if issue not in exact_entity_issues
        and _category(issue) not in (
            SAFE_SUMMARY_CATEGORIES
            | SAFE_TTS_CATEGORIES
            | SAFE_EVIDENCE_CATEGORIES
            | BLOCKED_CATEGORIES
        )
    ]
    if blocked or unknown:
        categories = sorted({_category(issue) or "unknown" for issue in blocked + unknown})
        return [], [
            "以下 high/critical 类别禁止无证据自动修复: " + ", ".join(categories)
        ]

    actions = []
    if exact_entity_issues:
        actions.append(_repair_exact_entities(folder, exact_entity_issues))
    categories = {_category(issue) for issue in high_issues}
    if categories & SAFE_SUMMARY_CATEGORIES:
        actions.append(_repair_summary(folder))
    if categories & SAFE_TTS_CATEGORIES:
        actions.append(_repair_tts(folder))
    evidence_issues = [
        issue for issue in high_issues
        if _category(issue) in SAFE_EVIDENCE_CATEGORIES
    ]
    if evidence_issues:
        actions.append(_repair_evidence(folder, evidence_issues))
    if not actions:
        return [], ["审查未通过，但没有可安全自动修复的 high/critical issue"]
    return actions, []


def review_and_repair(
        folder, *, max_rounds=2, reviewer=None,
        model="", effort="max", run_report=None):
    """Review, perform bounded safe repairs, and independently re-review."""
    folder = Path(folder)
    if reviewer is None:
        try:
            import ai_review
        except ImportError:
            from scripts import ai_review
        reviewer = ai_review.review_episode
    history = []
    review = None
    for round_index in range(max_rounds + 1):
        review_kwargs = {"model": model, "effort": effort}
        if run_report is not None:
            review_kwargs["run_report"] = run_report
        review = reviewer(folder, **review_kwargs)
        if not isinstance(review, dict):
            # Compatibility with injected/legacy reviewers that only perform
            # side effects. The caller's deterministic quality report remains
            # the source of truth and will decide whether publication passes.
            return review
        history.append({
            "round": round_index,
            "reviewed_at": review.get("reviewed_at"),
            "passed": bool(review.get("passed")),
            "reviewed_files": review.get("reviewed_files", {}),
        })
        if review.get("passed"):
            break
        if round_index >= max_rounds:
            break
        try:
            actions, blockers = repair_safe_issues(folder, review)
        except Exception as exc:
            actions, blockers = [], [str(exc)]
        history[-1]["repair_actions"] = actions
        history[-1]["repair_blockers"] = blockers
        if blockers or not actions:
            break

    payload = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "max_rounds": max_rounds,
        "passed": bool(review and review.get("passed")),
        "history": history,
    }
    atomic_write_json(folder / "review_repair.json", payload)
    return review

"""Bounded review/repair loop that never weakens review thresholds."""
import json
import re
from datetime import datetime, UTC
from pathlib import Path

from scripts.atomic_io import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)
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
from scripts.review_attribution import attribute_rejection
from scripts.subagent import run_edit_task


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
    "content_map.json", "中文完整笔记.md", "讲书稿.md", "tts_lexicon.json",
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
    # Repaired prose must bind the actual post-repair semantic inputs, not the
    # pre-review hashes, or the next normal resume would discard the repair.
    from scripts.agent_pipeline import WRITING_INPUTS_VERSION, _writing_input_hashes
    summary["writing_inputs_version"] = WRITING_INPUTS_VERSION
    summary["writing_inputs"] = _writing_input_hashes(folder)
    save_json(summary_path, summary)


def _repair_snapshot_paths(folder, issues):
    names = {
        "content_map.json",
        "summary_map.json",
        "中文完整笔记.md",
        "讲书稿.md",
        "tts_lexicon.json",
        PREWRITE_FACT_CHECKS_FILENAME,
    }
    for issue in issues:
        names.update(issue.get("allowed_files", []) or [])
    return [Path(folder) / name for name in sorted(names)]


def _restore_snapshots(snapshots):
    for path, original in snapshots.items():
        if original is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_bytes(path, original)


def _repair_exact_entities(folder, issues):
    folder = Path(folder)
    if any(
            "转录_纠错.txt" in (issue.get("allowed_files", []) or [])
            for issue in issues):
        raise RuntimeError(
            "结构化纠错稿不得由精确实体修复直接修改；"
            "请通过 correction manifest 纠错流程重新生成")
    raw_before = (folder / "原始转录.txt").read_bytes()
    transcript_before = (folder / "transcript.raw.json").read_bytes()
    snapshots = {
        path: path.read_bytes() if path.exists() else None
        for path in _repair_snapshot_paths(folder, issues)
    }
    replacements = []
    changes = []
    try:
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
    except Exception:
        _restore_snapshots(snapshots)
        raise
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
    _refresh_semantic_bindings(Path(folder), [])
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
    _refresh_semantic_bindings(Path(folder), [])
    return {
        "action": "refine_claim_evidence",
        "unit_ids": unit_ids,
        "claim_count": metrics.get("claim_count", 0),
    }


def _repair_content_revision(folder, review, *, model=""):
    """Ask pi to apply only review-listed prose fixes, then rebind the package."""
    folder = Path(folder)
    output_files = [
        folder / "中文完整笔记.md",
        folder / "讲书稿.md",
        folder / "summary_map.json",
    ]
    input_files = [
        folder / name
        for name in (
            "content_map.json", "canonical_entities.json",
            PREWRITE_FACT_CHECKS_FILENAME, "ai_review.json",
        )
        if (folder / name).exists()
    ]
    if not output_files or not input_files:
        raise RuntimeError("AI review revision 缺少写作输入或输出")
    issues = [
        issue for issue in review.get("issues", []) or []
        if isinstance(issue, dict)
        and issue.get("severity") in {"critical", "high"}
    ]
    if not issues:
        raise RuntimeError("AI review revision 没有明确的 critical/high 修复项")
    prompt = f"""只修复上一轮 AI review 明确列出的 critical/high 问题，随后返回并写入三份输出。
不得添加 transcript、content_map 或来源中不存在的新事实，不得修改证据、数字的来源归属、
实体证据、content_map.json、{PREWRITE_FACT_CHECKS_FILENAME} 或任何输入文件。
保持所有章节标题、summary_map 的 unit/claim 绑定和公开稿的自然中文；删除审查过程语言。
只允许修改中文完整笔记.md、讲书稿.md、summary_map.json。修复清单如下：
{json.dumps(issues, ensure_ascii=False)}
"""
    run_edit_task(
        folder,
        prompt,
        task_name="review_content_revision",
        allowed_files=output_files,
        input_files=input_files,
        required_files=output_files,
        model=model or None,
    )
    _refresh_semantic_bindings(folder, [])
    return {
        "action": "pi_review_content_revision",
        "issue_count": len(issues),
        "files": [path.name for path in output_files],
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
        from scripts import ai_review
        reviewer = ai_review.review_episode
    from scripts.ai_review import ReviewContractError
    from scripts.review_contract_repair import repair_contract_failure, failed_review_targets
    from scripts.ai_review import reviewed_hashes

    history = []
    review = None
    repair_scope = None
    pending_review = False
    cached_review = None
    current_hashes = reviewed_hashes(folder)
    review_path = folder / "ai_review.json"
    if review_path.exists():
        try:
            candidate = load_json(review_path)
            if candidate.get("reviewed_files") and candidate["reviewed_files"] == current_hashes:
                if candidate.get("passed"):
                    return candidate
                cached_review = candidate
        except (OSError, ValueError, TypeError):
            pass
    cached_contract = None
    if current_hashes:
        for path in sorted((folder / "ai_review_contract_failures").glob("*.json"),
                           key=lambda p: p.stat().st_mtime_ns, reverse=True):
            try:
                if (review_path.exists()
                        and path.stat().st_mtime_ns < review_path.stat().st_mtime_ns):
                    break
                data = load_json(path)
                if data.get("reviewed_files") == current_hashes:
                    cached_contract = ReviewContractError(
                        data.get("error", "review contract"), data)
                    cached_contract.diagnostic_path = path.relative_to(folder).as_posix()
                    break
            except (OSError, ValueError, TypeError):
                continue
    checkpoint = folder / "review_repair.json"
    if checkpoint.exists():
        try:
            pending = load_json(checkpoint).get("pending_re_review", {})
            if pending.get("reviewed_files") == current_hashes:
                pending_review = True
                repair_scope = pending.get("repair_scope")
        except (OSError, ValueError, TypeError):
            pass

    def checkpoint_re_review(scope):
        atomic_write_json(checkpoint, {
            "schema_version": 1, "passed": False, "max_rounds": max_rounds,
            "history": history,
            "pending_re_review": {"reviewed_files": reviewed_hashes(folder), "repair_scope": scope},
        })
    for round_index in range(max_rounds + 1):
        review_kwargs = {"model": model, "effort": effort}
        if run_report is not None:
            review_kwargs["run_report"] = run_report
        if repair_scope:
            review_kwargs["repair_scope"] = repair_scope
        try:
            if round_index == 0 and cached_contract is not None and not pending_review:
                raise cached_contract
            if round_index == 0 and cached_review is not None and not pending_review:
                review = cached_review
            else:
                review = reviewer(folder, **review_kwargs)
            repair_scope = None
        except ReviewContractError as exc:
            # A contract diagnostic is NOT a failed authoritative review. It
            # must never enter the normal safe-issue path or become a pass.
            entry = {
                "round": round_index, "passed": False,
                "contract_failure_snapshot": exc.diagnostic_path,
                "contract_error": str(exc), "repair_actions": [],
            }
            history.append(entry)
            try:
                if round_index >= max_rounds:
                    raise RuntimeError("来源/归因合同自动修复已达到轮数上限")
                action = repair_contract_failure(folder, exc.diagnostic, model=model, effort=effort)
                entry["repair_actions"].append(action)
                if action.get("affected_units"):
                    repair_scope = {"affected_units": action["affected_units"],
                                    "previous_review": exc.diagnostic.get("original_output", {})}
                checkpoint_re_review(repair_scope)
            except Exception as repair_exc:
                entry["repair_blockers"] = [str(repair_exc)]
                atomic_write_json(folder / "review_repair.json", {
                    "schema_version": 1, "completed_at": datetime.now(UTC).isoformat(),
                    "max_rounds": max_rounds, "passed": False, "history": history,
                })
                raise exc from repair_exc
            continue
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
        if not review.get("passed"):
            history[-1]["rejection"] = attribute_rejection(review)
        if review.get("passed"):
            break
        if round_index >= max_rounds:
            break
        try:
            targets = failed_review_targets(review)
            if targets:
                action = repair_contract_failure(folder, {"original_output": review,
                    "repair_targets": targets}, model=model, effort=effort)
                actions, blockers = [action], []
                repair_scope = {"affected_units": action["affected_units"],
                                "previous_review": review}
            else:
                actions, blockers = repair_safe_issues(folder, review)
        except Exception as exc:
            actions, blockers = [], [str(exc)]
        history[-1]["repair_actions"] = actions
        history[-1]["repair_blockers"] = blockers
        if not blockers and actions:
            checkpoint_re_review(repair_scope)
            continue
        # Safe deterministic repairs cannot address factuality, attribution,
        # or other prose findings.  Give pi one bounded, review-scoped revision
        # before the independent re-review; never turn a failed review into a
        # pass without another reviewer call.
        try:
            revision = _repair_content_revision(folder, review, model=model)
        except Exception as exc:
            history[-1].setdefault("repair_blockers", []).append(str(exc))
            break
        history[-1].setdefault("repair_actions", []).append(revision)
        checkpoint_re_review(None)

    payload = {
        "schema_version": 1,
        "completed_at": datetime.now(UTC).isoformat(),
        "max_rounds": max_rounds,
        "passed": bool(review and review.get("passed")),
        "history": history,
    }
    atomic_write_json(folder / "review_repair.json", payload)
    return review

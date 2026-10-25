"""Shared deterministic preflight for all artifact-producing entry points."""

import os
from pathlib import Path

from scripts.atomic_io import atomic_write_json
from scripts.source_relevance import (
    refresh_source_relevance_cache,
    repair_unconfirmed_source_citations,
)

from scripts.quality_errors import (
    AI_REVIEW_FAILED,
    AI_REVIEW_FACT_CHECK,
    AI_REVIEW_ISSUE_EVIDENCE,
    AI_REVIEW_MISSING,
    AI_REVIEW_SCORE,
    AI_REVIEW_SECTION,
    AI_REVIEW_SEVERE_ISSUE,
    AI_REVIEW_STALE,
    AUTO_REVIEW_CODES,
    ENTITY_ACCURACY_FAILED,
    quality_error_alignment,
)


def build_quality_report(*args, **kwargs):
    """Resolve the report builder lazily so callers can patch its module."""
    from scripts.quality_report import build_quality_report as builder

    return builder(*args, **kwargs)


def _review_recovery_decision(report):
    details = report.get("error_details")
    # Older resumed runs may have a report produced before structured error
    # details were added.  Do not let that bookkeeping defect suppress the
    # recovery that the report explicitly requests.
    aligned = quality_error_alignment(report)
    details_present = isinstance(details, list)
    details = details if details_present else []
    if details_present and not aligned:
        return False, False
    codes = [item.get("code") for item in details if isinstance(item, dict) and item.get("code")]
    if not details_present and not codes:
        messages = [str(message) for message in report.get("errors", [])]
        if any("ai_review.json" in message for message in messages):
            codes = [AI_REVIEW_MISSING]
        if any("内容审查状态未通过: pending" in message for message in messages):
            codes.append("content_review_status")
    if not codes:
        return False, False
    review_missing_or_stale = any(code in {AI_REVIEW_MISSING, AI_REVIEW_STALE} for code in codes)
    # A persisted failed review is actionable on resume too: the bounded
    # review→pi revision→independent re-review loop must not require a stale
    # hash to start repairing the rejected prose.
    failed_review = AI_REVIEW_FAILED in codes and AI_REVIEW_MISSING not in codes
    stale = AI_REVIEW_STALE in codes or failed_review
    stale_review_codes = AUTO_REVIEW_CODES | {
        AI_REVIEW_FAILED,
        AI_REVIEW_SECTION,
        AI_REVIEW_SCORE,
        AI_REVIEW_SEVERE_ISSUE,
        AI_REVIEW_FACT_CHECK,
        AI_REVIEW_ISSUE_EVIDENCE,
        ENTITY_ACCURACY_FAILED,
    }
    allowed_codes = stale_review_codes if stale else AUTO_REVIEW_CODES
    can_auto_review = (
        (review_missing_or_stale or failed_review)
        and bool(codes)
        and all(code in allowed_codes for code in codes)
    )
    return review_missing_or_stale, can_auto_review


def review_recovery_classification(report, previous=None):
    """Classify repeat failures without invoking a reviewer."""
    import hashlib
    import json

    details = report.get("error_details") or []
    codes = sorted(str(x.get("code")) for x in details if isinstance(x, dict) and x.get("code"))
    semantic = report.get("review_input_sha256") or report.get("input_sha256") or ""
    failure = hashlib.sha256(json.dumps(codes, sort_keys=True).encode()).hexdigest()
    current = {
        "semantic_input_hash": semantic,
        "failure_snapshot_hash": failure,
        "rejection_codes": codes,
    }
    if (
        previous
        and previous.get("semantic_input_hash") == semantic
        and previous.get("failure_snapshot_hash") == failure
    ):
        return "content_change_required", current
    if any(c in {"runner_timeout", "empty_output", "provider_rate_limit"} for c in codes):
        return "runner_transient", current
    if codes:
        return "checker_suspect", current
    return "ok", current


def quality_gate(
    folder,
    *,
    auto_ai_review=True,
    allow_legacy=False,
    run_report=None,
):
    """Run strict quality validation and optionally invoke the review agent."""
    folder = Path(folder)
    content_map = folder / "content_map.json"
    if not content_map.exists() and allow_legacy:
        print(
            "[质量门][兼容] 未找到 content_map.json；已通过显式参数允许旧期仅做结构校验",
            flush=True,
        )
        return True

    try:
        # Reuse current source evidence; forcing a network refresh here would
        # invalidate a valid review on every TTS/HTML retry.
        # The second refresh re-stamps the cache against the reduced reference
        # set before the quality report is built.
        relevance_cache = refresh_source_relevance_cache(folder)
        citation_repair = repair_unconfirmed_source_citations(
            folder, relevance_cache
        )
        if citation_repair.get("removed_urls"):
            print(
                "[质量门] 已移除刷新后仍无法确认的外部引用: "
                + ", ".join(citation_repair["removed_urls"]),
                flush=True,
            )
            refresh_source_relevance_cache(folder)
    except Exception as exc:
        print(
            f"[质量门][警告] 来源相关性缓存刷新/修复失败，将按现有缓存校验: {exc}",
            flush=True,
        )

    report = build_quality_report(folder, strict=True)
    out = folder / "quality_report.json"
    atomic_write_json(out, report)
    review_missing_or_stale, can_auto_review = _review_recovery_decision(report)
    if not report.get("passed", False) and auto_ai_review and can_auto_review:
        print("[质量门] AI 审查缺失或过期，自动运行 subagent...", flush=True)
        try:
            from scripts.review_repair import review_and_repair

            review_and_repair(
                folder,
                max_rounds=max(0, int(os.environ.get("REVIEW_REPAIR_MAX_ROUNDS", "2"))),
                model=os.environ.get("SUBAGENT_REVIEW_MODEL", ""),
                effort=os.environ.get("SUBAGENT_REVIEW_EFFORT", "max"),
                run_report=run_report,
            )
        except Exception as exc:
            print(f"[质量门][阻断] subagent AI 审查执行失败: {exc}", flush=True)
            return False
        report = build_quality_report(folder, strict=True)
        atomic_write_json(out, report)

    source_warning_counts = {}
    for warning in report.get("warnings", []):
        marker = "外部来源缓存暂时不可用，终审仍须独立核查: "
        if str(warning).startswith(marker):
            detail = str(warning)[len(marker) :]
            category = detail.split(":", 1)[0]
            source_warning_counts[category] = source_warning_counts.get(category, 0) + 1
            continue
        print(f"[质量门][警告] {warning}", flush=True)
    for category, count in sorted(source_warning_counts.items()):
        print(
            f"[质量门][警告] 外部来源缓存 {category}: {count} 项；终审仍须独立核查",
            flush=True,
        )
    if not report.get("passed", False):
        for error in report.get("errors", []):
            print(f"[质量门][阻断] {error}", flush=True)
        print(f"[质量门] 未通过，已写入 {out.name}，不会进入下游阶段", flush=True)
        return False
    print("[质量门] 内容完整性检查通过", flush=True)
    return True

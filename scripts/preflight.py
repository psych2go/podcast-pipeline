"""Shared deterministic preflight for all artifact-producing entry points."""
import os
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from pipeline_metrics import quality_metrics as _quality_metrics
    from quality_errors import (
        AI_REVIEW_MISSING, AI_REVIEW_STALE, AUTO_REVIEW_CODES,
        quality_error_alignment,
    )
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.pipeline_metrics import quality_metrics as _quality_metrics
    from scripts.quality_errors import (
        AI_REVIEW_MISSING, AI_REVIEW_STALE, AUTO_REVIEW_CODES,
        quality_error_alignment,
    )


def _review_recovery_decision(report):
    details = report.get("error_details")
    if not quality_error_alignment(report):
        return False, False
    codes = [
        item.get("code") for item in details
        if isinstance(item, dict) and item.get("code")
    ]
    review_missing_or_stale = any(
        code in {AI_REVIEW_MISSING, AI_REVIEW_STALE} for code in codes)
    can_auto_review = review_missing_or_stale and bool(codes) and all(
        code in AUTO_REVIEW_CODES for code in codes)
    return review_missing_or_stale, can_auto_review


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
            "[质量门][兼容] 未找到 content_map.json；"
            "已通过显式参数允许旧期仅做结构校验",
            flush=True,
        )
        return True

    try:
        from quality_report import build_quality_report
    except ImportError:
        from scripts.quality_report import build_quality_report
    report = build_quality_report(folder, strict=True)
    out = folder / "quality_report.json"
    atomic_write_json(out, report)
    review_missing_or_stale, can_auto_review = (
        _review_recovery_decision(report))
    if not report.get("passed", False) and auto_ai_review and can_auto_review:
        print("[质量门] AI 审查缺失或过期，自动运行 subagent...", flush=True)
        try:
            from review_repair import review_and_repair
            review_and_repair(
                folder,
                max_rounds=max(0, int(os.environ.get(
                    "REVIEW_REPAIR_MAX_ROUNDS", "2"))),
                model=os.environ.get("SUBAGENT_REVIEW_MODEL", ""),
                effort=os.environ.get("SUBAGENT_REVIEW_EFFORT", "max"),
                run_report=run_report,
            )
        except Exception as exc:
            print(f"[质量门][阻断] subagent AI 审查执行失败: {exc}", flush=True)
            return False
        report = build_quality_report(folder, strict=True)
        atomic_write_json(out, report)

    for warning in report.get("warnings", []):
        print(f"[质量门][警告] {warning}", flush=True)
    if not report.get("passed", False):
        for error in report.get("errors", []):
            print(f"[质量门][阻断] {error}", flush=True)
        print(f"[质量门] 未通过，已写入 {out.name}，不会进入下游阶段", flush=True)
        return False
    print("[质量门] 内容完整性检查通过", flush=True)
    return True

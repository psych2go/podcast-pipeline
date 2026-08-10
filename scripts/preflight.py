"""Shared deterministic preflight for all artifact-producing entry points."""
import json
import os
from pathlib import Path

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json


def _quality_metrics(folder):
    path = Path(folder) / "quality_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "passed": bool(report.get("passed")),
        "error_count": len(report.get("errors", [])),
        "warning_count": len(report.get("warnings", [])),
        "claim_coverage": report.get("coverage", {}).get("claim_coverage"),
        "notes_claim_coverage": report.get(
            "coverage", {}).get("notes_claim_coverage"),
    }


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
    review_only_prefixes = (
        "来源质量未通过自动关口:",
        "内容审查状态未通过:",
        "缺少 ai_review.json",
        "AI ",
    )
    review_missing_or_stale = any(
        error.startswith("缺少 ai_review.json")
        or error.startswith("AI 审查已过期")
        for error in report.get("errors", [])
    )
    can_auto_review = review_missing_or_stale and all(
        error.startswith(review_only_prefixes)
        for error in report["errors"]
    )
    if not report.get("passed", False) and auto_ai_review and can_auto_review:
        print("[质量门] AI 审查缺失或过期，自动运行 subagent...", flush=True)
        try:
            from ai_review import review_episode
            review_episode(
                folder,
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

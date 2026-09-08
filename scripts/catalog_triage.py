"""Read-only triage of quality-gate and AI-review blockers per episode.

Collects the audit artifacts a blocked episode already has (quality report,
current review, repair history, run-report ai_review stages, and retained
failure snapshots) and recommends the next *automated* action.  Nothing here
mutates episode state, rewrites prose, or flips verdicts.

Recommendation vocabulary (deliberately machine-actionable):

- ``rerun``            transient runner failure, or review missing/stale;
                       the standard entry point resumes on its own.
- ``content_regen``    review rejected; the next entry-point run rewrites
                       prose with the review feedback and re-reviews.
- ``checker_suspect``  consecutive rejections with unchanged semantic
                       inputs and repeated categories; regenerating will
                       not help, inspect the checker/prompt instead.
- ``gate_only``        review passed but deterministic quality codes block;
                       rerun the entry point to refresh bound artifacts.
- ``healthy``          nothing blocked.
"""
import json
from pathlib import Path

try:
    from quality_errors import AI_REVIEW_MISSING, AI_REVIEW_STALE
    from review_attribution import (
        OTHER_CATEGORY,
        attribute_rejection,
        normalize_category,
    )
except ImportError:
    from scripts.quality_errors import AI_REVIEW_MISSING, AI_REVIEW_STALE
    from scripts.review_attribution import (
        OTHER_CATEGORY,
        attribute_rejection,
        normalize_category,
)


# Review inputs whose hashes indicate a *semantic* change between attempts.
# episode.json / 来源.md are excluded on purpose: the review itself writes
# status transitions into them, so they differ after every attempt.
SEMANTIC_INPUT_FILES = (
    "原始转录.txt",
    "transcript.raw.json",
    "转录_纠错.txt",
    "content_map.json",
    "canonical_entities.json",
    "editorial_fact_checks.json",
    "中文完整笔记.md",
    "讲书稿.md",
    "summary_map.json",
    "tts_lexicon.json",
)

# Diagnostic markers for runner/service failures that are not rejections.
TRANSIENT_MARKERS = (
    "ERROR: unexpected status 502",
    '"code":"invalid_json_schema"',
    '"code": "invalid_json_schema"',
    "error: unexpected argument",
    "cannot be used multiple times",
    "Connection error",
    "Timeout",
)

ACTION_RERUN = "rerun"
ACTION_CONTENT_REGEN = "content_regen"
ACTION_CHECKER_SUSPECT = "checker_suspect"
ACTION_GATE_ONLY = "gate_only"
ACTION_HEALTHY = "healthy"


def _load_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _semantic_fingerprint(review):
    """Restrict reviewed_files to semantic inputs for static-input checks."""
    reviewed = review.get("reviewed_files")
    if not isinstance(reviewed, dict):
        return None
    return {
        name: reviewed.get(name)
        for name in SEMANTIC_INPUT_FILES
        if name in reviewed
    }


def _review_attempts(run_report):
    """Chronological ai_review stage records with structured rejections."""
    attempts = []
    for run in run_report.get("runs", []) or []:
        for stage in run.get("stages", []) or []:
            if stage.get("name") != "ai_review":
                continue
            attempts.append({
                "started_at": stage.get("started_at"),
                "status": stage.get("status"),
                "error": str(stage.get("error") or ""),
                "metrics": stage.get("metrics", {}) or {},
            })
    return attempts


def _is_transient(error):
    return any(marker in error for marker in TRANSIENT_MARKERS)


def _snapshot_reviews(folder):
    """Retained failure snapshots, oldest first by reviewed_at."""
    folder = Path(folder)
    snapshots = folder / "ai_review_failures"
    if not snapshots.is_dir():
        return []
    reviews = []
    for path in sorted(snapshots.glob("*.json")):
        review = _load_json(path)
        if review.get("passed") is False:
            review["_snapshot"] = path.name
            reviews.append(review)
    def _key(review):
        return str(review.get("reviewed_at") or review.get("_snapshot"))
    return sorted(reviews, key=_key)


def _loop_signals(folder, current_review):
    """Detect unchanged-input repeated rejections across consecutive failures.

    Uses retained failure snapshots (and the current failed review).  Older
    episodes without snapshots get ``static_inputs=None`` — unknown, never
    guessed, mirroring the honest-boundary style of past diagnostics.
    """
    failures = _snapshot_reviews(folder)
    if isinstance(current_review, dict) and current_review.get(
            "passed") is False:
        # The newest snapshot usually *is* the current review; comparing a
        # review with itself would trivially read as "unchanged inputs".
        # Deduplicate by reviewed_at before comparing.
        current_time = str(current_review.get("reviewed_at") or "")
        latest_snapshot_time = (
            str(failures[-1].get("reviewed_at") or "") if failures else "")
        if not failures or current_time != latest_snapshot_time:
            failures.append(dict(current_review, _snapshot="ai_review.json"))
    # Drop consecutive duplicates (same reviewed_at) among snapshots too.
    deduplicated = []
    for failure in failures:
        if deduplicated and str(
                failure.get("reviewed_at") or "") == str(
                deduplicated[-1].get("reviewed_at") or ""):
            continue
        deduplicated.append(failure)
    failures = deduplicated
    if len(failures) < 2:
        return {
            "static_inputs": None,
            "repeated_categories": [],
            "compared": [],
        }
    last, previous = failures[-1], failures[-2]
    last_fp = _semantic_fingerprint(last)
    previous_fp = _semantic_fingerprint(previous)
    static = (
        None if last_fp is None or previous_fp is None
        else last_fp == previous_fp
    )
    repeated = sorted(
        set(attribute_rejection(last).get("issue_counts", {}) or {})
        & set(attribute_rejection(previous).get("issue_counts", {}) or {})
    )
    return {
        "static_inputs": static,
        "repeated_categories": repeated,
        "compared": [previous.get("_snapshot"), last.get("_snapshot")],
    }


def _blocking_categories(review):
    attribution = attribute_rejection(review)
    return sorted(
        category for category in attribution.get("issue_counts", {})
        if category != OTHER_CATEGORY
    ) or ([OTHER_CATEGORY] if attribution.get("issue_counts") else [])


def _resume_command(name):
    return f".venv/bin/python scripts/process.py --name {json.dumps(name, ensure_ascii=False)}"


def triage_episode(folder):
    """Build one read-only triage report for a single episode folder."""
    folder = Path(folder)
    quality = _load_json(folder / "quality_report.json")
    review = _load_json(folder / "ai_review.json")
    repair = _load_json(folder / "review_repair.json")
    run_report = _load_json(folder / "run_report.json")

    quality_codes = []
    for item in quality.get("error_details", []) or []:
        if isinstance(item, dict) and item.get("code"):
            quality_codes.append(str(item["code"]))
    quality_passed = quality.get("passed") if quality else None
    review_passed = review.get("passed") if review else None

    attempts = _review_attempts(run_report)
    failed_attempts = [
        attempt for attempt in attempts if attempt.get("status") == "failed"]
    last_failed_error = (
        failed_attempts[-1].get("error", "") if failed_attempts else "")

    rejection = None
    if review_passed is False:
        rejection = attribute_rejection(review)

    loops = _loop_signals(folder, review if review else None)

    blocked = (
        quality_passed is False
        or review_passed is False
        or AI_REVIEW_MISSING in quality_codes
        or AI_REVIEW_STALE in quality_codes
        or not quality
    )

    evidence = []
    if review_passed is False:
        evidence.append("当前 ai_review.json passed=false")
    if quality_passed is False:
        evidence.append(
            f"quality_report.json 未通过（codes={quality_codes[:5]}）")
    if not quality:
        evidence.append("缺少 quality_report.json（尚未运行质量门）")

    # --- recommendation precedence (fully automated actions only) ---
    if not blocked:
        action, detail = ACTION_HEALTHY, "质量门与审查均为通过状态"
    elif not review:
        action, detail = (
            ACTION_RERUN,
            "缺少 AI 审查记录；下一次入口运行会自动审查并走质量门")
    elif AI_REVIEW_MISSING in quality_codes or AI_REVIEW_STALE in quality_codes:
        action, detail = (
            ACTION_RERUN,
            "审查缺失或已过期（输入哈希变化）；下一次入口运行会自动重审")
    elif _is_transient(last_failed_error):
        action, detail = (
            ACTION_RERUN,
            "最近一次失败是 runner/服务故障（502/schema/CLI 参数），"
            "不是内容判定；直接重跑即可")
    elif (
            loops.get("static_inputs") is True
            and loops.get("repeated_categories")):
        action = ACTION_CHECKER_SUSPECT
        detail = (
            "连续两次拒绝的语义输入完全未变且类别重复——重写不会改变结果，"
            "应先检查确定性检查器或提示词是否误报")
        evidence.append(
            "比较对象: " + " vs ".join(
                str(item) for item in loops.get("compared", []) if item))
        evidence.append(
            "重复类别: " + ", ".join(loops["repeated_categories"]))
    elif review_passed is False:
        action = ACTION_CONTENT_REGEN
        detail = (
            "下一次统一入口运行会把审查意见注入写作并自动重写，"
            "随后独立复审；无需人工改稿")
    else:
        action = ACTION_GATE_ONLY
        detail = (
            "审查已通过，质量门另有确定性错误码；"
            "重跑统一入口以刷新对应受审产物后自动复检")

    return {
        "episode": folder.name,
        "blocked": blocked,
        "review": {
            "exists": bool(review),
            "passed": review_passed,
            "reviewed_at": review.get("reviewed_at"),
            "rejection": rejection,
        },
        "quality": {
            "exists": bool(quality),
            "passed": quality_passed,
            "codes": quality_codes,
            "error_count": len(quality.get("errors", []) or []),
        },
        "attempts": {
            "total": len(attempts),
            "failed": len(failed_attempts),
            "last_error": last_failed_error,
        },
        "repair": {
            "rounds": len(repair.get("history", []) or []),
            "passed": repair.get("passed") if repair else None,
        },
        "loops": loops,
        "recommendation": {
            "action": action,
            "detail": detail,
            "resume_command": _resume_command(folder.name),
            "blocking_categories": (
                _blocking_categories(review)
                if review_passed is False else []),
            "evidence": evidence,
        },
    }


def triage_all(content_dir, blocked_only=False):
    """Triage every strict-mode episode folder, oldest name first."""
    content_dir = Path(content_dir)
    reports = []
    for folder in sorted(content_dir.iterdir()):
        if not folder.is_dir():
            continue
        episode = _load_json(folder / "episode.json")
        if episode.get("quality", {}).get("mode") == "legacy":
            continue
        report = triage_episode(folder)
        if blocked_only and not report["blocked"]:
            continue
        reports.append(report)
    return reports


def _md_cell(value):
    return str(value).replace("|", r"\|").replace("\n", " ")


def render_markdown(reports):
    lines = [
        "# Pipeline Triage", "",
        "只读诊断；不修改任何单集状态。", "",
    ]
    if not reports:
        lines.append("无符合条件的单集。")
        return "\n".join(lines) + "\n"
    blocked_count = sum(1 for report in reports if report["blocked"])
    lines.append(f"- 单集数：{len(reports)}（被阻断 {blocked_count}）")
    lines.append("")
    for report in reports:
        lines.append(f"## {_md_cell(report['episode'])}")
        lines.append("")
        state = "被阻断" if report["blocked"] else "正常"
        lines.append(f"- 状态：{state}")
        review = report["review"]
        if review["exists"]:
            verdict = (
                "passed" if review["passed"] else "rejected")
            lines.append(
                f"- 审查：{verdict}"
                + (f" @ {review['reviewed_at']}" if review["reviewed_at"]
                   else ""))
            rejection = review.get("rejection")
            if rejection:
                lines.append(
                    "- 拒绝原因码："
                    + ", ".join(rejection.get("codes", []) or ["无"]))
                if rejection.get("sections_failed"):
                    lines.append(
                        "- 失败分项："
                        + ", ".join(rejection["sections_failed"]))
                scores = {
                    key: value for key, value in (
                        rejection.get("scores", {}) or {}).items()
                    if value is not None
                }
                if scores:
                    lines.append("- 评分：" + ", ".join(
                        f"{key}={value}" for key, value in scores.items()))
                if rejection.get("issue_counts"):
                    lines.append("- issue 类别：" + ", ".join(
                        f"{key}×{value}" for key, value in sorted(
                            rejection["issue_counts"].items())))
        else:
            lines.append("- 审查：无记录")
        quality = report["quality"]
        if quality["exists"]:
            verdict = "passed" if quality["passed"] else "failed"
            codes = quality["codes"][:8]
            lines.append(
                f"- 质量门：{verdict}"
                + (f"（codes: {', '.join(codes)}）" if codes else ""))
        else:
            lines.append("- 质量门：未运行")
        attempts = report["attempts"]
        if attempts["total"]:
            lines.append(
                f"- 历史审查尝试：{attempts['total']} 次"
                f"（失败 {attempts['failed']}）")
        loops = report["loops"]
        if loops.get("static_inputs") is not None:
            lines.append(
                "- 循环检测：语义输入"
                + ("未变" if loops["static_inputs"] else "已变"))
            if loops.get("repeated_categories"):
                lines.append(
                    "- 重复拒绝类别：" + ", ".join(loops["repeated_categories"]))
        recommendation = report["recommendation"]
        lines.append(f"- 建议【{recommendation['action']}】：{recommendation['detail']}")
        if recommendation["blocking_categories"]:
            lines.append(
                "- 预期自动修复类别："
                + ", ".join(recommendation["blocking_categories"]))
        for item in recommendation["evidence"]:
            lines.append(f"  - 证据：{_md_cell(item)}")
        lines.append(
            f"  - 恢复命令：`{recommendation['resume_command']}`")
        lines.append("")
    return "\n".join(lines) + "\n"

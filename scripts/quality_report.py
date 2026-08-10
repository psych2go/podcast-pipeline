"""生成转录和中文讲稿的可审计质量报告。"""
import argparse
import json
import re
from pathlib import Path

try:
    from content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        coverage_report, load_json, validate_content_map, validate_summary_map,
    )
    from validator import structure_report
    from episode import quality_metadata
except ImportError:  # package import
    from scripts.content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        coverage_report, load_json, validate_content_map, validate_summary_map,
    )
    from scripts.validator import structure_report
    from scripts.episode import quality_metadata


MIN_NOTES_TO_BRIEFING_RATIO = 1.15
MIN_AI_REVIEW_SCORE = 90


def _zh_chars(text):
    return len(re.findall(r"[一-鿿]", text))


def _transcript_metrics(raw):
    segments = raw.get("segments", [])
    nonempty = [s for s in segments if (s.get("text") or "").strip()]
    speakers = sorted({s.get("speaker") for s in nonempty if s.get("speaker")})
    low_confidence = []
    for segment in nonempty:
        logprob = segment.get("avg_logprob")
        no_speech = segment.get("no_speech_prob")
        if logprob is not None and logprob < -1.5:
            low_confidence.append(segment)
        elif no_speech is not None and no_speech > 0.5:
            low_confidence.append(segment)
    timestamped = sum(
        1 for s in nonempty
        if s.get("start") is not None and s.get("end") is not None
    )
    return {
        "segment_count": len(nonempty),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "timestamped_segments": timestamped,
        "timestamp_coverage": round(timestamped / len(nonempty), 4) if nonempty else 0,
        "low_confidence_segments": len(low_confidence),
        "low_confidence_ratio": round(len(low_confidence) / len(nonempty), 4) if nonempty else 0,
        "transcript_chars": raw.get("meta", {}).get("transcript_chars"),
        "model": raw.get("meta", {}).get("model"),
        "quality": raw.get("meta", {}).get("quality"),
        "diarization": raw.get("meta", {}).get("diarization", False),
        "source_warnings": raw.get("meta", {}).get("source_warnings", []),
    }


def build_quality_report(folder, strict=True):
    folder = Path(folder)
    episode_quality = quality_metadata(folder)
    report = {
        "folder": str(folder),
        "strict": strict,
        "passed": True,
        "errors": [],
        "warnings": [],
    }
    raw_path = folder / "transcript.raw.json"
    if raw_path.exists():
        raw = load_json(raw_path)
        report["transcript"] = _transcript_metrics(raw)
        if report["transcript"]["timestamp_coverage"] < 0.95 and raw.get("source_kind") == "local_asr":
            report["warnings"].append("本地 ASR 时间戳覆盖率低于 95%")
        if report["transcript"]["low_confidence_ratio"] > 0.15:
            report["warnings"].append("低置信度片段超过 15%，建议人工复听")
        if report["transcript"]["source_warnings"]:
            report["warnings"].append("来源文本包含网页导语/链接/推荐内容，需人工确认正文边界")
    else:
        report["errors"].append("缺少 transcript.raw.json，无法进行完整转录审计")

    corrected_path = folder / "转录_纠错.txt"
    if corrected_path.exists():
        corrected_text = corrected_path.read_text(encoding="utf-8")
        report["corrected_transcript"] = {
            "file": corrected_path.name,
            "chars": len(corrected_text),
            "used_for_downstream_review": True,
        }

    briefing = next(iter(folder.glob("*讲书稿.md")), None)
    briefing_text = briefing.read_text(encoding="utf-8") if briefing else None
    notes_path = folder / "中文完整笔记.md"
    notes_text = (
        notes_path.read_text(encoding="utf-8")
        if notes_path.exists() else None
    )

    content_map_path = folder / "content_map.json"
    summary_map_path = folder / "summary_map.json"
    if content_map_path.exists():
        content_map = load_json(content_map_path)
        if (
                strict
                and content_map.get(
                    "schema_version", 1) < CONTENT_MAP_SCHEMA_VERSION):
            evidence_mode = episode_quality.get(
                "claim_evidence_mode", "precise_required")
            publish_report_path = folder / "publish_report.json"
            try:
                previously_published = bool(
                    load_json(publish_report_path).get("passed"))
            except (OSError, json.JSONDecodeError):
                previously_published = False
            if (
                    content_map.get("schema_version", 1) >= 2
                    and (
                        evidence_mode == "legacy_broad"
                        or previously_published
                    )):
                report["warnings"].append(
                    "该已发布单集仍使用 evidence v2 粗粒度 claim 证据；"
                    "新单集禁止使用此兼容模式")
            else:
                report["errors"].append(
                    "content_map.json 证据 schema 过旧，"
                    "需生成 v3 claim 级精确证据")
        raw_for_validation = load_json(raw_path) if raw_path.exists() else None
        errors, warnings = validate_content_map(
            content_map, raw_for_validation)
        report["content_map"] = {
            "unit_count": len(content_map.get("units", [])),
            "validation_errors": errors,
            "validation_warnings": warnings,
        }
        report["errors"].extend(errors)
        report["warnings"].extend(warnings)
        if summary_map_path.exists():
            summary_map = load_json(summary_map_path)
            if strict and summary_map.get("schema_version", 1) < 2:
                report["errors"].append(
                    "summary_map.json 仍是 v1，需运行 enrich-evidence")
            summary_errors = validate_summary_map(
                summary_map, briefing_text, content_map, notes_text)
            report["summary_map"] = {"validation_errors": summary_errors}
            report["errors"].extend(summary_errors)
            # 结构错误时不要继续调用 coverage_report，避免质量报告本身崩溃。
            if not errors and not summary_errors:
                coverage = coverage_report(content_map, summary_map)
                report["coverage"] = coverage
                if not coverage["passed"]:
                    report["errors"].append("总结覆盖率检查未通过")
        else:
            report["errors"].append("缺少 summary_map.json，无法验证讲稿覆盖率")
    else:
        message = "缺少 content_map.json，无法验证内容完整性"
        target = report["errors"] if strict else report["warnings"]
        target.append(message)

    if briefing:
        structure_warnings = structure_report(briefing_text)
        report["briefing"] = {
            "file": briefing.name,
            "zh_chars": _zh_chars(briefing_text),
            "chapters": len(re.findall(r"^## ", briefing_text, re.MULTILINE)),
            "structure_warnings": structure_warnings,
        }
        if strict:
            report["errors"].extend(
                f"讲稿结构未通过: {warning}"
                for warning in structure_warnings
            )
        else:
            report["warnings"].extend(structure_warnings)
        arabic_numbers = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", briefing_text)
        report["briefing"]["arabic_numbers"] = arabic_numbers
        if arabic_numbers:
            report["errors"].append(f"讲稿仍有阿拉伯数字，不适合直接 TTS: {arabic_numbers[:20]}")
    else:
        report["errors"].append("缺少讲书稿.md")

    if content_map_path.exists():
        if not notes_path.exists():
            report["errors"].append("缺少 中文完整笔记.md")
        else:
            notes_chars = _zh_chars(notes_text)
            briefing_chars = _zh_chars(briefing_text) if briefing else 0
            ratio = round(notes_chars / briefing_chars, 4) if briefing_chars else None
            report["complete_notes"] = {
                "zh_chars": notes_chars,
                "briefing_ratio": ratio,
            }
            if briefing and ratio < MIN_NOTES_TO_BRIEFING_RATIO:
                report["warnings"].append(
                    "完整笔记与精编讲稿字数接近；"
                    "发布依据改为 notes_claim_coverage，字数比例仅供观察："
                    f"{ratio:.2f} < {MIN_NOTES_TO_BRIEFING_RATIO:.2f}"
                )

        source_quality = episode_quality.get(
            "transcript_status", "未标注")
        report["source_quality"] = source_quality
        accepted = ("官方字幕", "可接受", "已纠错", "AI已审查（通过）")
        if not any(value in source_quality for value in accepted):
            report["errors"].append(f"来源质量未通过自动关口: {source_quality}")
        content_review_status = episode_quality.get(
            "content_review_status", "pending")
        report["content_review_status"] = content_review_status
        if content_review_status != "passed":
            report["errors"].append(
                "内容审查状态未通过: "
                f"{content_review_status}"
            )

        review_path = folder / "ai_review.json"
        if not review_path.exists():
            report["errors"].append("缺少 ai_review.json，不能自动发布")
        else:
            try:
                from ai_review import reviewed_hashes
            except ImportError:  # package import
                from scripts.ai_review import reviewed_hashes
            review = load_json(review_path)
            ai_errors = []
            if not review.get("passed"):
                ai_errors.append("AI 最终审查未通过")
            for section in ("transcript_quality", "coverage", "factuality", "numbers", "attribution", "tts", "publish"):
                if not review.get(section, {}).get("passed", False):
                    ai_errors.append(f"AI 审查分项未通过: {section}")
            for section in ("transcript_quality", "coverage", "factuality"):
                score = review.get(section, {}).get("score")
                if not isinstance(score, (int, float)) or score < MIN_AI_REVIEW_SCORE:
                    ai_errors.append(
                        f"AI 审查分数不足: {section}={score!r} < {MIN_AI_REVIEW_SCORE}"
                    )
            severe = [
                issue for issue in review.get("issues", [])
                if issue.get("severity") in {"critical", "high"}
            ]
            if severe:
                ai_errors.append(f"AI 审查仍有 {len(severe)} 个 critical/high 问题")
            if not isinstance(review.get("fact_checks"), list):
                ai_errors.append("AI 审查缺少可复现的 fact_checks")
            required_issue_evidence = {
                "evidence_type", "evidence_segment_ids",
                "source_urls", "checked_at",
            }
            incomplete_evidence = [
                index
                for index, issue in enumerate(review.get("issues", []))
                if not required_issue_evidence.issubset(issue)
            ]
            if incomplete_evidence:
                ai_errors.append(
                    "AI 审查 issue 缺少结构化证据字段: "
                    f"{incomplete_evidence}"
                )
            expected_hashes = review.get("reviewed_files", {})
            current_hashes = reviewed_hashes(folder)
            stale = sorted(
                name for name, digest in expected_hashes.items()
                if current_hashes.get(name) != digest
            )
            missing_hashes = sorted(set(current_hashes) - set(expected_hashes))
            if stale or missing_hashes:
                ai_errors.append(f"AI 审查已过期，文件变更: {stale + missing_hashes}")
            report["ai_review"] = {
                "passed": not ai_errors,
                "errors": ai_errors,
                "reviewed_at": review.get("reviewed_at"),
                "summary": review.get("summary"),
            }
            report["errors"].extend(ai_errors)

    report["passed"] = not report["errors"]
    return report


def main():
    parser = argparse.ArgumentParser(description="生成播客质量报告")
    parser.add_argument("folder")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    report = build_quality_report(args.folder, strict=True)
    out = args.out or str(Path(args.folder) / "quality_report.json")
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

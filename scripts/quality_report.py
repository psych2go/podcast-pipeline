"""生成转录和中文讲稿的可审计质量报告。"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json

try:
    from content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        body_sha256, content_map_evidence_mode, coverage_report, load_json,
        transcript_evidence_mode, validate_content_map, validate_summary_map,
    )
    from validator import structure_report
    from episode import inspect_episode_state
    from evidence import (
        ASR_SOURCE_KINDS,
        correction_metrics,
        effective_source_kind,
        validate_provenance,
    )
except ImportError:  # package import
    from scripts.content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        body_sha256, content_map_evidence_mode, coverage_report, load_json,
        transcript_evidence_mode, validate_content_map, validate_summary_map,
    )
    from scripts.validator import structure_report
    from scripts.episode import inspect_episode_state
    from scripts.evidence import (
        ASR_SOURCE_KINDS,
        correction_metrics,
        effective_source_kind,
        validate_provenance,
    )


MIN_NOTES_TO_BRIEFING_RATIO = 1.15
MIN_AI_REVIEW_SCORE = 90


def _zh_chars(text):
    return len(re.findall(r"[一-鿿]", text))


def _transcript_status_accepted(value):
    return str(value or "").startswith(
        ("官方字幕", "可接受", "已纠错", "AI已审查（通过）"))


def _find_briefing(folder):
    folder = Path(folder)
    for candidate in (
            folder / "讲书稿.md",
            folder / f"{folder.name} - 讲书稿.md"):
        if candidate.exists():
            return candidate
    hits = sorted(folder.glob("*讲书稿.md"))
    return hits[0] if hits else None


def _transcript_metrics(raw):
    segments = raw.get("segments", [])
    meta = raw.get("meta", {})
    refinement = meta.get("adaptive_refinement", {})
    if not isinstance(refinement, dict):
        refinement = {}
    alignment = meta.get("alignment", {})
    if not isinstance(alignment, dict):
        alignment = {}
    diarization_meta = meta.get("diarization_meta", {})
    if not isinstance(diarization_meta, dict):
        diarization_meta = {}
    nonempty = [s for s in segments if (s.get("text") or "").strip()]
    needs_review = sum(
        bool(
            segment.get("needs_redecode")
            or segment.get("needs_review")
            or segment.get("speaker_alignment") == "unresolved"
        )
        for segment in nonempty
    )
    recorded_remaining = refinement.get("remaining_segments")
    remaining_segments = max(
        needs_review,
        int(recorded_remaining)
        if isinstance(recorded_remaining, (int, float)) else 0,
    )
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
        "evidence_mode": transcript_evidence_mode(raw),
        "low_confidence_segments": len(low_confidence),
        "low_confidence_ratio": round(len(low_confidence) / len(nonempty), 4) if nonempty else 0,
        "needs_redecode_segments": needs_review,
        "adaptive_refinement": {
            "enabled": bool(refinement.get("enabled")),
            "candidate_ranges": refinement.get("candidate_ranges", 0),
            "accepted_ranges": refinement.get("accepted_ranges", 0),
            "rejected_ranges": refinement.get("rejected_ranges", 0),
            "failed_ranges": refinement.get("failed_ranges", 0),
            "remaining_segments": remaining_segments,
        },
        "alignment": {
            "enabled": bool(alignment.get("enabled")),
            "adapter": alignment.get("adapter"),
            "status": alignment.get("status"),
            "model": alignment.get("model"),
            "device": alignment.get("device"),
            "word_timestamp_coverage": alignment.get(
                "word_timestamp_coverage"),
            "warning": meta.get("alignment_warning"),
        },
        "transcript_chars": meta.get("transcript_chars"),
        "model": meta.get("model"),
        "quality": meta.get("quality"),
        "diarization": meta.get("diarization", False),
        "diarization_warning": meta.get("diarization_warning"),
        "diarization_model": (
            meta.get("diarization_model")
            or diarization_meta.get("model")
        ),
        "diarization_exclusive": bool(
            meta.get(
                "diarization_exclusive",
                diarization_meta.get("exclusive_used", False),
            )
        ),
        "language": meta.get("language"),
        "language_probability": meta.get("language_probability"),
        "requested_language": meta.get("requested_language"),
        "synthetic_boundary_segments": sum(
            bool(segment.get("synthetic_boundary"))
            for segment in nonempty
        ),
        "source_warnings": meta.get("source_warnings", []),
        "tls_downgrade": meta.get("tls_downgrade", False),
        "tls_downgrade_reason": meta.get(
            "tls_downgrade_reason"),
    }


def _ai_fact_check_consistency(review):
    fact_checks = review.get("fact_checks")
    if not isinstance(fact_checks, list):
        return ["AI 审查缺少可复现的 fact_checks"], []
    errors = []
    warnings = []
    unsupported_used = [
        item.get("claim")
        for item in fact_checks
        if item.get("verdict") == "unsupported"
        and item.get("publication_status") == "used_as_fact"
    ]
    if unsupported_used:
        errors.append(
            "AI fact_checks 存在未获支持但仍作为事实采用的内容: "
            f"{unsupported_used}")
    unsupported_without_status = [
        item.get("claim")
        for item in fact_checks
        if item.get("verdict") == "unsupported"
        and not item.get("publication_status")
    ]
    if unsupported_without_status:
        warnings.append(
            "旧版 AI fact_checks 缺少 publication_status，"
            "无法机械判断 unsupported 内容是否进入发布稿")
    return errors, warnings


def build_quality_report(folder, strict=True):
    folder = Path(folder)
    episode_quality = inspect_episode_state(folder)
    report = {
        "folder": str(folder),
        "strict": strict,
        "passed": True,
        "errors": [],
        "warnings": [],
    }
    raw_path = folder / "transcript.raw.json"
    raw = None
    if raw_path.exists():
        raw = load_json(raw_path)
        report["transcript"] = _transcript_metrics(raw)
        source_kind = effective_source_kind(folder, raw)
        report["transcript"]["effective_source_kind"] = source_kind
        provenance_errors, provenance_warnings = validate_provenance(
            folder, raw)
        report["errors"].extend(provenance_errors)
        report["warnings"].extend(provenance_warnings)
        evidence = raw.get("evidence", {})
        transcript_path = folder / evidence.get(
            "transcript_file", raw.get("meta", {}).get(
                "transcript_file", "原始转录.txt"))
        expected_transcript_hash = evidence.get("transcript_sha256")
        if expected_transcript_hash:
            if not transcript_path.exists():
                report["errors"].append(
                    f"原始 evidence 缺少 {transcript_path.name}")
            else:
                actual_hash = hashlib.sha256(
                    transcript_path.read_bytes()).hexdigest()
                if actual_hash != expected_transcript_hash:
                    report["errors"].append(
                        "原始转录与 transcript.raw.json 的 evidence hash 不一致")

        is_local_asr = source_kind in ASR_SOURCE_KINDS
        asr_quality = report["transcript"]["quality"]
        strict_asr = (
            source_kind == "local_asr"
            and asr_quality in {"balanced", "max"}
        )
        if (
                is_local_asr
                and report["transcript"]["timestamp_coverage"] < 0.95):
            target = report["errors"] if strict_asr else report["warnings"]
            target.append("本地 ASR 时间戳覆盖率低于 95%")
        if (
                is_local_asr
                and report["transcript"]["low_confidence_ratio"] > 0.15):
            target = report["errors"] if strict_asr else report["warnings"]
            target.append("本地 ASR 低置信度片段超过 15%")
        language_probability = report["transcript"]["language_probability"]
        if (
                is_local_asr
                and isinstance(language_probability, (int, float))
                and language_probability < 0.6):
            target = report["errors"] if strict_asr else report["warnings"]
            target.append(
                f"ASR 语言识别置信度过低: {language_probability:.2f} < 0.60")
        if report["transcript"]["diarization_warning"]:
            report["warnings"].append(
                "说话人分离已降级跳过: "
                f"{report['transcript']['diarization_warning']}")
        alignment = report["transcript"]["alignment"]
        if is_local_asr and alignment["warning"]:
            report["warnings"].append(
                "ASR 强制对齐已回退到 Whisper 时间戳: "
                f"{alignment['warning']}")
        alignment_coverage = alignment["word_timestamp_coverage"]
        if (
                is_local_asr
                and alignment["enabled"]
                and isinstance(alignment_coverage, (int, float))
                and alignment_coverage < 0.9):
            report["warnings"].append(
                "ASR 强制对齐词时间戳覆盖率低于 90%")
        refinement = report["transcript"]["adaptive_refinement"]
        if is_local_asr and refinement["failed_ranges"]:
            report["warnings"].append(
                "ASR 定向重解码存在失败区间: "
                f"{refinement['failed_ranges']} 段")
        if is_local_asr and refinement["remaining_segments"]:
            report["warnings"].append(
                "ASR 定向重解码后仍有待复核片段: "
                f"{refinement['remaining_segments']} 段")
        if report["transcript"]["synthetic_boundary_segments"]:
            report["warnings"].append(
                "转录包含无时间戳文本的合成分段，segment 仅用于字符级定位")
        if report["transcript"]["tls_downgrade"]:
            report["errors"].append(
                "转录来源抓取时关闭了 TLS 证书校验，严格模式拒绝发布")
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
            "used_for_downstream_review": False,
        }
        drift = correction_metrics(folder)
        if drift:
            report["corrected_transcript"]["drift"] = drift
    if (
            raw
            and effective_source_kind(folder, raw) in ASR_SOURCE_KINDS
            and not corrected_path.exists()):
        report["errors"].append(
            "ASR 单集缺少 转录_纠错.txt，不能进入下游发布")

    briefing = _find_briefing(folder)
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
            if (
                    content_map.get("schema_version", 1) >= 2
                    and evidence_mode == "legacy_broad"):
                report["warnings"].append(
                    "该已发布单集仍使用 evidence v2 粗粒度 claim 证据；"
                    "新单集禁止使用此兼容模式")
            else:
                report["errors"].append(
                    "content_map.json 证据 schema 过旧，"
                    "需生成 v3 claim 级精确证据")
        raw_for_validation = load_json(raw_path) if raw_path.exists() else None
        transcript_mode = (
            transcript_evidence_mode(raw_for_validation)
            if raw_for_validation else "timestamp"
        )
        map_mode = content_map_evidence_mode(content_map, raw_for_validation)
        if map_mode != transcript_mode:
            report["errors"].append(
                "content_map.evidence_mode 与 transcript.raw.json "
                "的 evidence_mode 不一致"
            )
        errors, warnings = validate_content_map(
            content_map, raw_for_validation)
        report["content_map"] = {
            "unit_count": len(content_map.get("units", [])),
            "evidence_mode": map_mode,
            "validation_errors": errors,
            "validation_warnings": warnings,
        }
        refiner = content_map.get("claim_evidence_refiner")
        if isinstance(refiner, dict):
            report["content_map"]["claim_evidence_refiner"] = {
                "command": refiner.get("command"),
                "model": refiner.get("model"),
                "effort": refiner.get("effort"),
                "batch_count": refiner.get("batch_count"),
                "refined_at": content_map.get(
                    "claim_evidence_refined_at"),
            }
        has_multi_claim_units = any(
            len(unit.get("claims", [])) > 1
            for unit in content_map.get("units", [])
        )
        if (
                content_map.get(
                    "schema_version", 1) >= CONTENT_MAP_SCHEMA_VERSION
                and has_multi_claim_units
                and not content_map.get("claim_evidence_refined_at")):
            report["warnings"].append(
                "evidence v3 含多 claim 单元，但缺少 claim evidence 精炼时间元数据")
        report["errors"].extend(errors)
        report["warnings"].extend(warnings)
        if summary_map_path.exists():
            summary_map = load_json(summary_map_path)
            if strict and summary_map.get("schema_version", 1) < 2:
                report["errors"].append(
                    "summary_map.json 仍是 v1，需运行 enrich-evidence")
            summary_errors = validate_summary_map(
                summary_map, briefing_text, content_map, notes_text)
            basis = summary_map.get("transcript_basis")
            if isinstance(basis, dict):
                basis_file = basis.get("file")
                if basis_file not in {"原始转录.txt", "转录_纠错.txt"}:
                    summary_errors.append(
                        f"summary_map.transcript_basis.file 无效: "
                        f"{basis_file!r}")
                else:
                    basis_path = folder / basis_file
                    if not basis_path.exists():
                        summary_errors.append(
                            f"summary_map 声明的转录依据不存在: {basis_file}")
                    elif basis.get("sha256") != body_sha256(
                            basis_path.read_text(encoding="utf-8")):
                        summary_errors.append(
                            "summary_map.transcript_basis.sha256 已过期")
            if (
                    raw
                    and effective_source_kind(
                        folder, raw) in ASR_SOURCE_KINDS):
                if not corrected_path.exists():
                    summary_errors.append(
                        "ASR 单集缺少 转录_纠错.txt，不能进入下游发布")
                elif not isinstance(basis, dict):
                    summary_errors.append(
                        "ASR 单集缺少 summary_map.transcript_basis")
                elif basis.get("file") != corrected_path.name:
                    summary_errors.append(
                        "ASR 单集的讲稿依据必须绑定 转录_纠错.txt")
            report["summary_map"] = {"validation_errors": summary_errors}
            if isinstance(basis, dict):
                report["summary_map"]["transcript_basis"] = basis
                if (
                        corrected_path.exists()
                        and basis.get("file") == corrected_path.name
                        and basis.get("sha256") == body_sha256(
                            corrected_path.read_text(encoding="utf-8"))):
                    report["corrected_transcript"][
                        "used_for_downstream_review"] = True
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
        if not _transcript_status_accepted(source_quality):
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
            fact_check_errors, fact_check_warnings = (
                _ai_fact_check_consistency(review))
            ai_errors.extend(fact_check_errors)
            report["warnings"].extend(fact_check_warnings)
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
                "transcript_quality": {
                    key: review.get("transcript_quality", {}).get(key)
                    for key in (
                        "score", "raw_score", "corrected_score",
                        "accuracy_basis",
                    )
                    if key in review.get("transcript_quality", {})
                },
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
    atomic_write_json(out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

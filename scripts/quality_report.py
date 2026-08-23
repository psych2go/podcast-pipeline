"""生成转录和中文讲稿的可审计质量报告。"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from quality_errors import (
        ASR_QUALITY_FAILED, BRIEFING_AUDIT_NARRATION, BRIEFING_MISSING, BRIEFING_STRUCTURE_FAILED,
        CLAIM_EVIDENCE_FALLBACK, CONTENT_MAP_MISSING,
        CONTENT_MAP_MODE_MISMATCH, COVERAGE_FAILED, NOTES_AUDIT_NARRATION, NOTES_MISSING,
        SOURCE_QUALITY_FAILED, SUMMARY_MAP_MISSING, SUMMARY_MAP_SCHEMA,
        TRANSCRIPT_CORRECTION_MISSING, TRANSCRIPT_INTEGRITY_FAILED,
        TRANSCRIPT_MISSING,
        AI_REVIEW_FAILED, AI_REVIEW_FACT_CHECK, AI_REVIEW_ISSUE_EVIDENCE,
        AI_REVIEW_MISSING, AI_REVIEW_SCORE, AI_REVIEW_SECTION,
        AI_REVIEW_SEVERE_ISSUE, AI_REVIEW_STALE, CONTENT_MAP_SCHEMA,
        CONTENT_MAP_VALIDATION, CONTENT_REVIEW_STATUS, ENTITY_ACCURACY_FAILED,
        EVIDENCE_PROVENANCE_FAILED, SOURCE_REVIEW_STATUS,
        SUMMARY_MAP_VALIDATION, TTS_READINESS_FAILED, add_error, coded_errors,
        extend_errors, quality_error_alignment,
    )
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.quality_errors import (
        ASR_QUALITY_FAILED, BRIEFING_AUDIT_NARRATION, BRIEFING_MISSING, BRIEFING_STRUCTURE_FAILED,
        CLAIM_EVIDENCE_FALLBACK, CONTENT_MAP_MISSING,
        CONTENT_MAP_MODE_MISMATCH, COVERAGE_FAILED, NOTES_AUDIT_NARRATION, NOTES_MISSING,
        SOURCE_QUALITY_FAILED, SUMMARY_MAP_MISSING, SUMMARY_MAP_SCHEMA,
        TRANSCRIPT_CORRECTION_MISSING, TRANSCRIPT_INTEGRITY_FAILED,
        TRANSCRIPT_MISSING,
        AI_REVIEW_FAILED, AI_REVIEW_FACT_CHECK, AI_REVIEW_ISSUE_EVIDENCE,
        AI_REVIEW_MISSING, AI_REVIEW_SCORE, AI_REVIEW_SECTION,
        AI_REVIEW_SEVERE_ISSUE, AI_REVIEW_STALE, CONTENT_MAP_SCHEMA,
        CONTENT_MAP_VALIDATION, CONTENT_REVIEW_STATUS, ENTITY_ACCURACY_FAILED,
        EVIDENCE_PROVENANCE_FAILED, SOURCE_REVIEW_STATUS,
        SUMMARY_MAP_VALIDATION, TTS_READINESS_FAILED, add_error, coded_errors,
        extend_errors, quality_error_alignment,
    )

try:
    from content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        body_sha256, content_map_evidence_mode, coverage_report, load_json,
        transcript_evidence_mode, unit_claim_ids, validate_content_map,
        validate_summary_map,
    )
    from validator import audit_narration_issues, structure_report
    from episode import (
        LEGACY_EVIDENCE_READ_CUTOFF, inspect_episode_state,
        legacy_evidence_frozen_before_cutoff, legacy_evidence_read_allowed,
    )
    from evidence import (
        ASR_SOURCE_KINDS,
        correction_metrics,
        effective_source_kind,
        validate_provenance,
    )
    from content_finalizer import validate_tts_readiness
    from canonical_entities import (
        public_entity_alias_errors,
        validate_canonical_entities,
    )
    from claim_evidence import PROGRESS_FILENAME, validate_progress
    from editorial_corrections import (
        load_editorial_corrections,
        validate_editorial_corrections,
    )
    from source_relevance import (
        CACHE_FILENAME as SOURCE_RELEVANCE_CACHE,
        expected_source_references,
        validate_source_relevance_cache,
    )
    from tts import load_tts_lexicon
    from claim_taxonomy import validate_review_fact_checks
except ImportError:  # package import
    from scripts.content_map import (
        CONTENT_MAP_SCHEMA_VERSION,
        body_sha256, content_map_evidence_mode, coverage_report, load_json,
        transcript_evidence_mode, unit_claim_ids, validate_content_map,
        validate_summary_map,
    )
    from scripts.validator import audit_narration_issues, structure_report
    from scripts.episode import (
        LEGACY_EVIDENCE_READ_CUTOFF, inspect_episode_state,
        legacy_evidence_frozen_before_cutoff, legacy_evidence_read_allowed,
    )
    from scripts.evidence import (
        ASR_SOURCE_KINDS,
        correction_metrics,
        effective_source_kind,
        validate_provenance,
    )
    from scripts.content_finalizer import validate_tts_readiness
    from scripts.canonical_entities import (
        public_entity_alias_errors,
        validate_canonical_entities,
    )
    from scripts.claim_evidence import PROGRESS_FILENAME, validate_progress
    from scripts.editorial_corrections import (
        load_editorial_corrections,
        validate_editorial_corrections,
    )
    from scripts.source_relevance import (
        CACHE_FILENAME as SOURCE_RELEVANCE_CACHE,
        expected_source_references,
        validate_source_relevance_cache,
    )
    from scripts.tts import load_tts_lexicon
    from scripts.claim_taxonomy import validate_review_fact_checks


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


def _ai_fact_check_consistency_v3(review, valid_claim_ids=None):
    return validate_review_fact_checks(
        review, valid_claim_ids=valid_claim_ids)


def _ai_fact_check_consistency(review, valid_claim_ids=None):
    fact_checks = review.get("fact_checks")
    if not isinstance(fact_checks, list):
        return ["AI 审查缺少可复现的 fact_checks"], []
    errors = []
    warnings = []
    schema_version = int(review.get("schema_version", 1) or 1)
    if schema_version >= 3:
        return _ai_fact_check_consistency_v3(
            review, valid_claim_ids=valid_claim_ids)
    required_v2 = {"claim_type", "verification_mode"}

    for index, item in enumerate(fact_checks):
        if not isinstance(item, dict):
            errors.append(f"AI fact_checks[{index}] 必须是对象")
            continue
        claim = item.get("claim") or f"fact_checks[{index}]"
        missing_v2 = sorted(required_v2 - set(item))
        if missing_v2:
            target = errors if schema_version >= 2 else warnings
            target.append(
                f"{claim}: AI fact_check 缺少 claim 分类字段 {missing_v2}")
            # Preserve legacy review compatibility after reporting the gap.
            if schema_version < 2:
                if (
                        item.get("verdict") == "unsupported"
                        and item.get("publication_status") == "used_as_fact"):
                    errors.append(
                        "AI fact_checks 存在未获支持但仍作为事实采用的内容: "
                        f"{claim}")
                continue

        claim_type = item.get("claim_type")
        mode = item.get("verification_mode")
        verdict = item.get("verdict")
        status = item.get("publication_status")
        segment_ids = item.get("evidence_segment_ids")
        source_urls = item.get("source_urls")
        segment_ids = segment_ids if isinstance(segment_ids, list) else []
        source_urls = source_urls if isinstance(source_urls, list) else []

        if verdict == "unsupported" and status == "used_as_fact":
            errors.append(
                "AI fact_checks 存在未获支持但仍作为事实采用的内容: "
                f"{claim}")
        if verdict == "faithfully_attributed" and status not in {
                "attributed_or_qualified", "excluded"}:
            errors.append(
                f"{claim}: faithfully_attributed 内容不能作为无归因事实采用")
        if verdict == "not_applicable" and status == "used_as_fact":
            errors.append(
                f"{claim}: not_applicable 内容不能作为客观事实采用")

        if claim_type == "guest_firsthand":
            if mode != "transcript_attribution":
                errors.append(
                    f"{claim}: guest_firsthand 必须使用 transcript_attribution")
            if status == "used_as_fact":
                errors.append(
                    f"{claim}: 嘉宾一手信息必须明确归因，不能标记 used_as_fact")
            if status != "excluded" and not segment_ids:
                errors.append(
                    f"{claim}: 嘉宾一手信息缺少转录 segment 证据")
            if status != "excluded" and verdict not in {
                    "faithfully_attributed", "qualified"}:
                errors.append(
                    f"{claim}: 嘉宾一手信息应判 faithfully_attributed 或 qualified")
        elif claim_type == "guest_opinion":
            if mode not in {"not_applicable", "transcript_attribution"}:
                errors.append(
                    f"{claim}: guest_opinion 核查模式不应要求外部事实证明")
            if status == "used_as_fact":
                errors.append(
                    f"{claim}: 嘉宾观点不能升级为客观事实")
            if status != "excluded" and not segment_ids:
                errors.append(f"{claim}: 嘉宾观点缺少转录归因证据")
            if status != "excluded" and verdict not in {
                    "faithfully_attributed", "not_applicable", "qualified"}:
                errors.append(f"{claim}: 嘉宾观点 verdict 与归因状态不一致")
        elif claim_type == "public_fact":
            if (
                    status == "used_as_fact"
                    and verdict in {"supported", "qualified"}
                    and mode == "web_required"
                    and not source_urls):
                errors.append(f"{claim}: 公开事实缺少网页来源")
        elif claim_type == "editorial_fact":
            if status == "used_as_fact" and mode != "web_required":
                errors.append(f"{claim}: 编辑部新增事实必须使用 web_required")
            if (
                    status == "used_as_fact"
                    and verdict in {"supported", "qualified"}
                    and not source_urls):
                errors.append(f"{claim}: 编辑部新增事实缺少网页来源")
        elif claim_type == "editorial_inference":
            if status == "used_as_fact":
                errors.append(f"{claim}: 编辑推论必须明确限定，不能写成既定事实")

    unsupported_without_status = [
        item.get("claim")
        for item in fact_checks
        if isinstance(item, dict)
        and item.get("verdict") == "unsupported"
        and not item.get("publication_status")
    ]
    if unsupported_without_status:
        warnings.append(
            "旧版 AI fact_checks 缺少 publication_status，"
            "无法机械判断 unsupported 内容是否进入发布稿")
    return errors, warnings


def _ai_entity_accuracy_consistency(review):
    section = review.get("entity_accuracy")
    schema_version = int(review.get("schema_version", 1) or 1)
    if not isinstance(section, dict):
        message = "AI 审查缺少 entity_accuracy 实体准确性分项"
        return ([message], []) if schema_version >= 2 else ([], [message])
    errors = []
    warnings = []
    if not section.get("passed", False):
        errors.append("AI 实体准确性审查未通过")
    checks = section.get("checked_entities")
    if not isinstance(checks, list):
        target = errors if schema_version >= 2 else warnings
        target.append("AI entity_accuracy 缺少 checked_entities")
        return errors, warnings
    incorrect = [
        item.get("observed")
        for item in checks
        if isinstance(item, dict) and item.get("verdict") == "incorrect"
    ]
    if incorrect:
        errors.append(f"AI 实体核查仍有错误名称或归属: {incorrect}")
    uncertain = [
        item.get("observed")
        for item in checks
        if isinstance(item, dict) and item.get("verdict") == "uncertain"
    ]
    if uncertain:
        warnings.append(f"AI 实体核查仍有不确定项: {uncertain}")
    return errors, warnings


def build_quality_report(folder, strict=True, *, today=None):
    folder = Path(folder)
    episode_quality = inspect_episode_state(folder)
    report = {
        "folder": str(folder),
        "strict": strict,
        "passed": True,
        "errors": [],
        "error_details": [],
        "warnings": [],
    }
    coded_errors(report)
    raw_path = folder / "transcript.raw.json"
    raw = None
    if raw_path.exists():
        raw = load_json(raw_path)
        report["transcript"] = _transcript_metrics(raw)
        source_kind = effective_source_kind(folder, raw)
        report["transcript"]["effective_source_kind"] = source_kind
        provenance_errors, provenance_warnings = validate_provenance(
            folder, raw)
        extend_errors(report, EVIDENCE_PROVENANCE_FAILED, provenance_errors)
        report["warnings"].extend(provenance_warnings)
        evidence = raw.get("evidence", {})
        transcript_path = folder / evidence.get(
            "transcript_file", raw.get("meta", {}).get(
                "transcript_file", "原始转录.txt"))
        expected_transcript_hash = evidence.get("transcript_sha256")
        if expected_transcript_hash:
            if not transcript_path.exists():
                add_error(
                    report, TRANSCRIPT_INTEGRITY_FAILED,
                    f"原始 evidence 缺少 {transcript_path.name}")
            else:
                actual_hash = hashlib.sha256(
                    transcript_path.read_bytes()).hexdigest()
                if actual_hash != expected_transcript_hash:
                    add_error(
                        report, TRANSCRIPT_INTEGRITY_FAILED,
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
            if strict_asr:
                add_error(report, ASR_QUALITY_FAILED,
                          "本地 ASR 时间戳覆盖率低于 95%")
            else:
                report["warnings"].append("本地 ASR 时间戳覆盖率低于 95%")
        if (
                is_local_asr
                and report["transcript"]["low_confidence_ratio"] > 0.15):
            if strict_asr:
                add_error(report, ASR_QUALITY_FAILED,
                          "本地 ASR 低置信度片段超过 15%")
            else:
                report["warnings"].append("本地 ASR 低置信度片段超过 15%")
        language_probability = report["transcript"]["language_probability"]
        if (
                is_local_asr
                and isinstance(language_probability, (int, float))
                and language_probability < 0.6):
            message = (
                f"ASR 语言识别置信度过低: {language_probability:.2f} < 0.60")
            if strict_asr:
                add_error(report, ASR_QUALITY_FAILED, message)
            else:
                report["warnings"].append(message)
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
            add_error(
                report, TRANSCRIPT_INTEGRITY_FAILED,
                "转录来源抓取时关闭了 TLS 证书校验，严格模式拒绝发布")
        if report["transcript"]["source_warnings"]:
            report["warnings"].append("来源文本包含网页导语/链接/推荐内容，需人工确认正文边界")
    else:
        add_error(report, TRANSCRIPT_MISSING, "缺少 transcript.raw.json，无法进行完整转录审计")

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
        add_error(
            report, TRANSCRIPT_CORRECTION_MISSING,
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
        entities_path = folder / "canonical_entities.json"
        if content_map.get("canonical_entities_contract_version") == 1:
            if not entities_path.exists():
                entity_errors = ["缺少 canonical_entities.json"]
            else:
                entity_payload = load_json(entities_path)
                entity_errors = validate_canonical_entities(
                    entity_payload, raw or {})
                entity_errors.extend(public_entity_alias_errors(
                    entity_payload, notes_text, briefing_text))
            report["canonical_entities"] = {
                "validation_errors": entity_errors,
            }
            extend_errors(
                report, CONTENT_MAP_VALIDATION, entity_errors)
        corrections_path = folder / "editorial_corrections.json"
        if corrections_path.exists():
            correction_errors = validate_editorial_corrections(
                load_editorial_corrections(corrections_path),
                valid_claim_ids=set(unit_claim_ids(
                    content_map, include_excluded=True)),
            )
            report["editorial_corrections"] = {
                "validation_errors": correction_errors,
            }
            extend_errors(
                report, CONTENT_MAP_VALIDATION, correction_errors)
        relevance_path = folder / SOURCE_RELEVANCE_CACHE
        if relevance_path.exists():
            relevance_payload = load_json(relevance_path)
            relevance_errors = validate_source_relevance_cache(
                relevance_payload, expected_source_references(folder))
            report["source_relevance_cache"] = {
                "validation_errors": relevance_errors,
                "entry_count": len(relevance_payload.get("entries", {})),
            }
            extend_errors(
                report, CONTENT_MAP_VALIDATION, relevance_errors)
        progress_path = folder / PROGRESS_FILENAME
        if progress_path.exists():
            progress_payload = load_json(progress_path)
            progress_errors = validate_progress(
                progress_payload, raw or {})
            report["claim_evidence_progress"] = {
                "status": progress_payload.get("status"),
                "validation_errors": progress_errors,
            }
            extend_errors(
                report, CONTENT_MAP_VALIDATION, progress_errors)
        if (
                strict
                and content_map.get(
                    "schema_version", 1) < CONTENT_MAP_SCHEMA_VERSION):
            evidence_mode = episode_quality.get(
                "claim_evidence_mode", "precise_required")
            legacy_v2 = (
                content_map.get("schema_version", 1) >= 2
                and evidence_mode == "legacy_broad"
            )
            frozen_history = (
                legacy_v2 and legacy_evidence_frozen_before_cutoff(folder)
            )
            if (
                    legacy_v2
                    and frozen_history
                    and legacy_evidence_read_allowed(today)):
                report["warnings"].append(
                    "该冻结日前已发布单集仍使用只读 evidence v2；"
                    "兼容将于 "
                    f"{LEGACY_EVIDENCE_READ_CUTOFF.isoformat()} 停止，"
                    "届时必须迁移到 v3 claim 级精确证据")
            else:
                if legacy_v2 and not frozen_history:
                    message = (
                        "evidence v2 缺少冻结日前成功发布证明；"
                        "禁止通过 episode.json 手工标记启用兼容")
                elif legacy_v2:
                    message = (
                        "evidence v2 兼容已于 "
                        f"{LEGACY_EVIDENCE_READ_CUTOFF.isoformat()} 停止；"
                        "需生成 v3 claim 级精确证据")
                else:
                    message = (
                        "content_map.json 证据 schema 过旧，"
                        "需生成 v3 claim 级精确证据")
                add_error(report, CONTENT_MAP_SCHEMA, message)
        raw_for_validation = load_json(raw_path) if raw_path.exists() else None
        transcript_mode = (
            transcript_evidence_mode(raw_for_validation)
            if raw_for_validation else "timestamp"
        )
        map_mode = content_map_evidence_mode(content_map, raw_for_validation)
        if map_mode != transcript_mode:
            add_error(
                report, CONTENT_MAP_MODE_MISMATCH,
                "content_map.evidence_mode 与 transcript.raw.json "
                "的 evidence_mode 不一致")
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
            if (
                    "deterministic-fallback" in str(refiner.get("command", ""))
                    or int(refiner.get("fallback_claim_count", 0) or 0) > 0):
                add_error(
                    report, CLAIM_EVIDENCE_FALLBACK,
                    "claim evidence 使用 deterministic fallback，严格发布禁止降级证据")
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
        extend_errors(report, CONTENT_MAP_VALIDATION, errors)
        report["warnings"].extend(warnings)
        if summary_map_path.exists():
            summary_map = load_json(summary_map_path)
            if strict and summary_map.get("schema_version", 1) < 2:
                add_error(
                    report, SUMMARY_MAP_SCHEMA,
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
            extend_errors(report, SUMMARY_MAP_VALIDATION, summary_errors)
            # 结构错误时不要继续调用 coverage_report，避免质量报告本身崩溃。
            if not errors and not summary_errors:
                coverage = coverage_report(content_map, summary_map)
                report["coverage"] = coverage
                if not coverage["passed"]:
                    add_error(report, COVERAGE_FAILED, "总结覆盖率检查未通过")
        else:
            add_error(report, SUMMARY_MAP_MISSING, "缺少 summary_map.json，无法验证讲稿覆盖率")
    else:
        message = "缺少 content_map.json，无法验证内容完整性"
        if strict:
            add_error(report, CONTENT_MAP_MISSING, message)
        else:
            report["warnings"].append(message)

    if briefing:
        structure_warnings = structure_report(briefing_text)
        report["briefing"] = {
            "file": briefing.name,
            "zh_chars": _zh_chars(briefing_text),
            "chapters": len(re.findall(r"^## ", briefing_text, re.MULTILINE)),
            "structure_warnings": structure_warnings,
        }
        if strict:
            extend_errors(
                report, BRIEFING_STRUCTURE_FAILED,
                (f"讲稿结构未通过: {warning}"
                 for warning in structure_warnings))
        else:
            report["warnings"].extend(structure_warnings)
        audit_issues = audit_narration_issues(briefing_text)
        report["briefing"]["audit_narration_issues"] = audit_issues
        if strict:
            extend_errors(
                report, BRIEFING_AUDIT_NARRATION,
                (f"讲稿含面向内部的审查过程语言: {issue}"
                 for issue in audit_issues))
        else:
            report["warnings"].extend(audit_issues)
        arabic_numbers = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", briefing_text)
        report["briefing"]["arabic_numbers"] = arabic_numbers
        tts_readiness_issues = validate_tts_readiness(
            briefing_text, load_tts_lexicon(folder))
        report["briefing"]["tts_readiness_issues"] = tts_readiness_issues
        extend_errors(report, TTS_READINESS_FAILED, tts_readiness_issues)
    else:
        add_error(report, BRIEFING_MISSING, "缺少讲书稿.md")

    if content_map_path.exists():
        if not notes_path.exists():
            add_error(report, NOTES_MISSING, "缺少 中文完整笔记.md")
        else:
            notes_chars = _zh_chars(notes_text)
            briefing_chars = _zh_chars(briefing_text) if briefing else 0
            ratio = round(notes_chars / briefing_chars, 4) if briefing_chars else None
            notes_audit_issues = audit_narration_issues(notes_text)
            report["complete_notes"] = {
                "zh_chars": notes_chars,
                "briefing_ratio": ratio,
                "audit_narration_issues": notes_audit_issues,
            }
            if strict:
                extend_errors(
                    report, NOTES_AUDIT_NARRATION,
                    (f"完整笔记含面向内部的审查过程语言: {issue}"
                     for issue in notes_audit_issues))
            else:
                report["warnings"].extend(notes_audit_issues)
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
            add_error(
                report, SOURCE_REVIEW_STATUS,
                f"来源质量未通过自动关口: {source_quality}")
        content_review_status = episode_quality.get(
            "content_review_status", "pending")
        report["content_review_status"] = content_review_status
        if content_review_status != "passed":
            add_error(
                report, CONTENT_REVIEW_STATUS,
                "内容审查状态未通过: "
                f"{content_review_status}")

        review_path = folder / "ai_review.json"
        if not review_path.exists():
            add_error(report, AI_REVIEW_MISSING, "缺少 ai_review.json，不能自动发布")
        else:
            try:
                from ai_review import reviewed_hashes
            except ImportError:  # package import
                from scripts.ai_review import reviewed_hashes
            review = load_json(review_path)
            ai_errors = []
            ai_error_details = []

            def ai_error(code, message):
                ai_errors.append(message)
                ai_error_details.append((code, message))

            if not review.get("passed"):
                ai_error(AI_REVIEW_FAILED, "AI 最终审查未通过")
            for section in ("transcript_quality", "coverage", "factuality", "numbers", "attribution", "tts", "publish"):
                if not review.get(section, {}).get("passed", False):
                    ai_error(AI_REVIEW_SECTION, f"AI 审查分项未通过: {section}")
            for section in ("transcript_quality", "coverage", "factuality"):
                score = review.get(section, {}).get("score")
                if not isinstance(score, (int, float)) or score < MIN_AI_REVIEW_SCORE:
                    ai_error(
                        AI_REVIEW_SCORE,
                        f"AI 审查分数不足: {section}={score!r} < {MIN_AI_REVIEW_SCORE}")
            severe = [
                issue for issue in review.get("issues", [])
                if issue.get("severity") in {"critical", "high"}
            ]
            if severe:
                ai_error(AI_REVIEW_SEVERE_ISSUE, f"AI 审查仍有 {len(severe)} 个 critical/high 问题")
            fact_check_errors, fact_check_warnings = (
                _ai_fact_check_consistency(
                    review,
                    valid_claim_ids=set(unit_claim_ids(
                        content_map, include_excluded=True)),
                ))
            for message in fact_check_errors:
                ai_error(AI_REVIEW_FACT_CHECK, message)
            report["warnings"].extend(fact_check_warnings)
            entity_errors, entity_warnings = (
                _ai_entity_accuracy_consistency(review))
            for message in entity_errors:
                ai_error(ENTITY_ACCURACY_FAILED, message)
            report["warnings"].extend(entity_warnings)
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
                ai_error(
                    AI_REVIEW_ISSUE_EVIDENCE,
                    "AI 审查 issue 缺少结构化证据字段: "
                    f"{incomplete_evidence}")
            expected_hashes = review.get("reviewed_files", {})
            current_hashes = reviewed_hashes(folder)
            stale = sorted(
                name for name, digest in expected_hashes.items()
                if current_hashes.get(name) != digest
            )
            missing_hashes = sorted(set(current_hashes) - set(expected_hashes))
            if stale or missing_hashes:
                ai_error(AI_REVIEW_STALE, f"AI 审查已过期，文件变更: {stale + missing_hashes}")
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
            for code, message in ai_error_details:
                add_error(report, code, message)

    if not quality_error_alignment(report):
        raise RuntimeError("quality report errors/error_details 不一致")
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

"""
内容单元台账和总结覆盖率工具。

本模块不调用 LLM。subagent/人工可以根据提示词生成 content_map.json 和
summary_map.json，本模块负责 schema 校验、覆盖率统计和失败阻断。
"""
import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json

try:
    from sections import chapter_body_map
except ImportError:
    from scripts.sections import chapter_body_map


STATUS_VALUES = {
    "pending", "included", "condensed", "excluded", "unresolved",
    "needs_review", "unsupported",
}
EXCLUSION_TYPES = {
    "advertisement", "housekeeping", "banter", "non_speech",
    "duplicate", "technical_noise", "other",
}
SOURCE_ACCOUNTABILITY_VERSION = 1
IMPORTANCE_VALUES = {"high", "medium", "low"}
CONTENT_MAP_SCHEMA_VERSION = 3
SUMMARY_MAP_SCHEMA_VERSION = 2
DETAIL_ITEMS_VERSION = 1
CLAIM_CONFIDENCE_VALUES = {"high", "medium", "low"}
CLAIM_MODALITIES = {
    "actual_event", "conditional", "prediction", "opinion",
    "recommendation", "general_claim",
}
EVIDENCE_MODES = {"timestamp", "text_anchor"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, payload):
    atomic_write_json(path, payload)


def ensure_segment_ids(raw):
    for index, segment in enumerate(raw.get("segments", []), start=1):
        segment.setdefault("id", f"S{index:04d}")
    return raw


def transcript_evidence_mode(transcript):
    """Return the explicit source evidence mode, with timestamp compatibility."""
    meta = transcript.get("meta", {}) or {}
    mode = meta.get("evidence_mode")
    if mode in EVIDENCE_MODES:
        return mode
    if meta.get("timestamped") is False:
        return "text_anchor"
    if meta.get("timestamped") is True:
        return "timestamp"
    segments = transcript.get("segments", [])
    if segments and any(
            segment.get("start") is not None
            and segment.get("end") is not None
            for segment in segments):
        return "timestamp"
    return "text_anchor"


def content_map_evidence_mode(payload, transcript=None):
    mode = payload.get("evidence_mode")
    if mode in EVIDENCE_MODES:
        return mode
    if transcript is not None:
        return transcript_evidence_mode(transcript)
    return "timestamp"


def segment_evidence_sha256(segments, segment_ids):
    wanted = set(segment_ids)
    lines = [
        f"{segment.get('id')}\n{(segment.get('text') or '').strip()}"
        for segment in segments
        if segment.get("id") in wanted
    ]
    return hashlib.sha256(
        "\n\n".join(lines).encode("utf-8")).hexdigest()


def _segments_for_timestamps(segments, timestamps):
    selected = []
    for window in timestamps or []:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError(f"timestamp 格式无效: {window!r}")
        window_start, window_end = window
        if (
                window_start is None
                or window_end is None
                or window_start < 0
                or window_end < window_start):
            raise ValueError(f"timestamp 范围无效: {window!r}")
    for segment in segments:
        start = segment.get("start")
        end = segment.get("end")
        if start is None:
            continue
        end = end if end is not None else start
        for window in timestamps or []:
            if not isinstance(window, list) or len(window) != 2:
                continue
            window_start, window_end = window
            if window_start is None or window_end is None:
                continue
            segment_point = end == start
            window_point = window_end == window_start
            if segment_point:
                overlaps = window_start <= start <= window_end
            elif window_point:
                overlaps = start <= window_start < end
            else:
                overlaps = end > window_start and start < window_end
            if overlaps:
                selected.append(segment)
                break
    return selected


def enrich_content_map_evidence(content_map, transcript):
    """Refresh unit evidence without inventing multi-claim evidence."""
    transcript = ensure_segment_ids(transcript)
    segments = transcript.get("segments", [])
    mode = content_map_evidence_mode(content_map, transcript)
    content_map["evidence_mode"] = mode
    for unit in content_map.get("units", []):
        unit_id = unit.get("id") or "unknown-unit"
        previous_ids = list(
            unit.get("evidence", {}).get("segment_ids", []) or [])
        if mode == "text_anchor":
            existing_ids = set(
                unit.get("evidence", {}).get("segment_ids", []))
            selected = [
                segment for segment in segments
                if segment.get("id") in existing_ids
            ]
        else:
            open_anchor_starts = {
                segment.get("start")
                for segment in segments
                if segment.get("start") is not None
                and segment.get("end") is None
            }
            normalized_timestamps = []
            for timestamp in unit.get("timestamps", []):
                if not isinstance(timestamp, list) or len(timestamp) != 2:
                    normalized_timestamps.append(timestamp)
                    continue
                start, end = timestamp
                normalized_timestamps.append(
                    [start, start]
                    if (
                        start is not None and end is None
                        and start in open_anchor_starts
                    )
                    else [start, end]
                )
            unit["timestamps"] = normalized_timestamps
            selected = _segments_for_timestamps(
                segments, normalized_timestamps)
        segment_ids = [
            segment["id"] for segment in selected if segment.get("id")]
        if not segment_ids:
            raise ValueError(
                f"{unit_id}: evidence enrichment 结果为空；"
                f"previous_segment_ids={previous_ids!r}, "
                f"timestamps={unit.get('timestamps', [])!r}")
        unit["evidence"] = {
            "mode": mode,
            "segment_ids": segment_ids,
            "source_sha256": segment_evidence_sha256(
                segments, segment_ids),
        }
        previous = unit.get("claim_evidence", {})
        previous_notes = unit.get("claim_evidence_notes", {})
        claim_evidence = {}
        claim_hashes = {}
        claim_notes = {}
        claims = unit.get("claims", [])
        for index, _claim in enumerate(claims, start=1):
            claim_key = f"C{index:02d}"
            existing = previous.get(claim_key)
            if (
                    isinstance(existing, list)
                    and existing
                    and set(existing).issubset(set(segment_ids))):
                selected_ids = list(dict.fromkeys(existing))
            elif len(claims) == 1:
                selected_ids = segment_ids
            else:
                selected_ids = []
            claim_evidence[claim_key] = selected_ids
            if selected_ids:
                claim_hashes[claim_key] = segment_evidence_sha256(
                    segments, selected_ids)
            note = previous_notes.get(claim_key)
            if isinstance(note, dict):
                claim_notes[claim_key] = note
            elif len(claims) == 1 and selected_ids:
                claim_notes[claim_key] = {
                    "confidence": "high",
                    "rationale": "单 claim 单元，证据范围与单元证据一致。",
                }
        unit["claim_evidence"] = claim_evidence
        unit["claim_evidence_sha256"] = claim_hashes
        unit["claim_evidence_notes"] = claim_notes
    content_map["schema_version"] = CONTENT_MAP_SCHEMA_VERSION
    return content_map, transcript


def apply_claim_evidence_mapping(
        content_map, transcript, mappings, unit_ids=None):
    """Apply reviewed claim-level segment mappings and bind their hashes."""
    transcript = ensure_segment_ids(transcript)
    segments = transcript.get("segments", [])
    selected_unit_ids = set(unit_ids or [])
    mapping_by_id = {
        item.get("claim_id"): item
        for item in mappings
        if isinstance(item, dict) and item.get("claim_id")
    }
    for unit in content_map.get("units", []):
        unit_id = unit.get("id")
        if selected_unit_ids and unit_id not in selected_unit_ids:
            continue
        unit_segments = set(
            unit.get("evidence", {}).get("segment_ids", []))
        evidence = {}
        roles = {}
        hashes = {}
        notes = {}
        for index, _claim in enumerate(unit.get("claims", []), start=1):
            key = f"C{index:02d}"
            full_id = f"{unit_id}-{key}"
            item = mapping_by_id.get(full_id, {})
            segment_ids = list(dict.fromkeys(item.get("segment_ids", [])))
            if not segment_ids:
                raise ValueError(f"{full_id}: 未返回 claim 证据")
            if not set(segment_ids).issubset(unit_segments):
                raise ValueError(f"{full_id}: claim 证据不属于单元证据")
            confidence = item.get("confidence")
            rationale = str(item.get("rationale", "")).strip()
            if confidence not in CLAIM_CONFIDENCE_VALUES:
                raise ValueError(f"{full_id}: confidence 无效")
            if not rationale:
                raise ValueError(f"{full_id}: 缺少 evidence rationale")
            evidence[key] = segment_ids
            primary = list(dict.fromkeys(
                item.get("primary_segment_ids", segment_ids)))
            context = [
                segment_id
                for segment_id in dict.fromkeys(
                    item.get("context_segment_ids", []))
                if segment_id not in set(primary)
            ]
            if not primary:
                raise ValueError(f"{full_id}: primary_segment_ids 不能为空")
            if primary + context != segment_ids:
                raise ValueError(f"{full_id}: primary/context 与 claim 证据不一致")
            roles[key] = {
                "primary_segment_ids": primary,
                "context_segment_ids": context,
            }
            hashes[key] = segment_evidence_sha256(segments, segment_ids)
            notes[key] = {
                "confidence": confidence,
                "rationale": rationale,
            }
        unit["claim_evidence"] = evidence
        unit["claim_evidence_roles"] = roles
        unit["claim_evidence_sha256"] = hashes
        unit["claim_evidence_notes"] = notes
    content_map["schema_version"] = CONTENT_MAP_SCHEMA_VERSION
    content_map["claim_evidence_refined_at"] = datetime.now(
        timezone.utc).isoformat()
    return content_map, transcript


def enrich_summary_map_evidence(
        summary_map, notes_text, content_map, briefing_text=None):
    normalize_summary_claim_ids(summary_map)
    summary_map["schema_version"] = SUMMARY_MAP_SCHEMA_VERSION
    summary_map["notes_sha256"] = body_sha256(notes_text)
    # Coverage is authored by the summarizer/reviewer. Never manufacture the
    # complete claim set here: doing so would let summary_map self-certify that
    # the prose actually contains every claim.
    summary_map.setdefault("notes_claim_ids", [])
    if content_map.get("detail_items_version") == DETAIL_ITEMS_VERSION:
        summary_map.setdefault("notes_number_ids", [])
        summary_map.setdefault("notes_example_ids", [])
    normalize_summary_claim_ids(summary_map)
    if briefing_text is not None:
        chapters = briefing_chapters(briefing_text)
        for chapter in summary_map.get("chapters", []):
            title = chapter.get("title")
            if title in chapters:
                chapter["body_sha256"] = body_sha256(chapters[title])
    return summary_map


def normalize_claim_id(value):
    """Return the canonical Uxxxx-Cxx claim identifier when recognizable."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(U\d{4,})[.:\-](C\d{2,})", text, re.IGNORECASE)
    if not match:
        return text
    return f"{match.group(1).upper()}-{match.group(2).upper()}"


def normalize_summary_claim_ids(summary_map):
    """Normalize common subagent claim-id variants in place."""
    if not isinstance(summary_map, dict):
        return summary_map
    for chapter in summary_map.get("chapters", []) or []:
        if not isinstance(chapter, dict):
            continue
        claim_ids = chapter.get("claim_ids")
        if isinstance(claim_ids, list):
            chapter["claim_ids"] = [
                normalize_claim_id(claim_id) for claim_id in claim_ids
            ]
    notes_claim_ids = summary_map.get("notes_claim_ids")
    if isinstance(notes_claim_ids, list):
        summary_map["notes_claim_ids"] = [
            normalize_claim_id(claim_id) for claim_id in notes_claim_ids
        ]
    return summary_map


def init_content_map(transcript_json, output, title=""):
    """从结构化转录创建空的、可人工补充的内容台账模板。"""
    raw = ensure_segment_ids(load_json(transcript_json))
    mode = transcript_evidence_mode(raw)
    units = []
    for index, segment in enumerate(raw.get("segments", []), start=1):
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = segment.get("start")
        end = segment.get("end")
        if mode == "timestamp" and start is not None and end is None:
            end = start
        timestamps = (
            [[start, end]]
            if mode == "timestamp" else []
        )
        units.append({
            "id": f"U{index:04d}",
            "topic": "",
            "speaker": segment.get("speaker"),
            "claims": [],
            "reasoning": [],
            "examples": [],
            "numbers": [],
            "terms": [],
            "timestamps": timestamps,
            "source_excerpt": text,
            "evidence": {
                "mode": mode,
                "segment_ids": [segment["id"]],
                "source_sha256": segment_evidence_sha256(
                    raw.get("segments", []), [segment["id"]]),
            },
            "claim_evidence": {},
            "importance": "medium",
            "status": "pending",
            "notes": "",
        })
    payload = {
        "schema_version": CONTENT_MAP_SCHEMA_VERSION,
        "source_accountability_version": SOURCE_ACCOUNTABILITY_VERSION,
        "evidence_mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "source_transcript": str(transcript_json),
        "instructions": "请将相邻片段合并为完整话题，并补充 claims/reasoning/examples/numbers/terms。",
        "units": units,
    }
    save_json(output, payload)
    return payload


def source_accountability_contract_required(transcript):
    meta = (transcript or {}).get("meta", {}) or {}
    value = meta.get("source_accountability_contract_version", 0)
    if value in (None, ""):
        return False
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            "source_accountability_contract_version 必须是非负整数")
    return value >= SOURCE_ACCOUNTABILITY_VERSION


def source_segment_accountability(payload, transcript):
    """Account for every nonempty source segment without forcing publication."""
    transcript = ensure_segment_ids(transcript or {"segments": []})
    source_ids = [
        str(segment.get("id"))
        for segment in transcript.get("segments", [])
        if (segment.get("text") or "").strip()
    ]
    references = []
    valid_references = []
    invalid_unit_ids = []
    status_counts = Counter()
    for index, unit in enumerate(payload.get("units", []) or []):
        if not isinstance(unit, dict):
            invalid_unit_ids.append(f"units[{index}]")
            continue
        status = str(unit.get("status"))
        status_counts[status] += 1
        evidence = unit.get("evidence")
        unit_references = (
            [str(value) for value in evidence.get("segment_ids", [])]
            if isinstance(evidence, dict) else []
        )
        references.extend(unit_references)
        valid = status in {"included", "condensed"}
        if status == "excluded":
            valid = bool(unit.get("notes")) and unit.get(
                "exclusion_type") in EXCLUSION_TYPES
            if unit.get("exclusion_type") == "other":
                valid = valid and len(re.sub(
                    r"\s+", "", str(unit.get("notes", "")))) >= 10
        if valid:
            valid_references.extend(unit_references)
        else:
            invalid_unit_ids.append(str(unit.get("id") or f"units[{index}]"))
    reference_counts = Counter(valid_references)
    source_set = set(source_ids)
    missing = [segment_id for segment_id in source_ids if segment_id not in reference_counts]
    duplicates = sorted(
        segment_id for segment_id, count in reference_counts.items()
        if segment_id in source_set and count > 1
    )
    unknown = sorted(set(references) - source_set)
    covered = len(source_ids) - len(missing)
    return {
        "total": len(source_ids),
        "covered": covered,
        "coverage": round(covered / len(source_ids), 4) if source_ids else 1.0,
        "missing_ids": missing,
        "duplicate_ids": duplicates,
        "unknown_ids": unknown,
        "invalid_unit_ids": sorted(set(invalid_unit_ids)),
        "status_counts": dict(sorted(status_counts.items())),
        "passed": not missing and not unknown and not invalid_unit_ids,
    }


def validate_content_map(payload, transcript=None):
    """校验内容台账是否已经完成人工/LLM 整理。"""
    errors = []
    warnings = []
    units = payload.get("units")
    if not isinstance(units, list):
        return ["units 必须是数组"], []
    if not units:
        return ["units 不能为空：没有可核验的内容单元"], []

    ids = set()
    transcript_segments = []
    transcript_ids = set()
    synthetic_transcript_ids = set()
    transcript_max_end = None
    synthetic_warning_added = False
    declared_mode = payload.get("evidence_mode")
    if declared_mode is not None and declared_mode not in EVIDENCE_MODES:
        errors.append(f"evidence_mode 无效: {declared_mode!r}")
    evidence_mode = content_map_evidence_mode(payload, transcript)
    if transcript is not None:
        transcript = ensure_segment_ids(transcript)
        transcript_declared_mode = (
            transcript.get("meta", {}) or {}).get("evidence_mode")
        if (
                transcript_declared_mode is not None
                and transcript_declared_mode not in EVIDENCE_MODES):
            errors.append(
                "transcript.raw.json.meta.evidence_mode 无效: "
                f"{transcript_declared_mode!r}"
            )
        transcript_mode = transcript_evidence_mode(transcript)
        try:
            accountability_required = source_accountability_contract_required(
                transcript)
        except ValueError as exc:
            accountability_required = True
            errors.append(str(exc))
        if accountability_required and payload.get(
                "source_accountability_version") != SOURCE_ACCOUNTABILITY_VERSION:
            errors.append(
                "新 evidence revision 要求 source_accountability_version=1")
        if (
                payload.get("evidence_mode")
                and payload.get("evidence_mode") != transcript_mode
        ):
            errors.append(
                "content_map.evidence_mode 与 transcript.raw.json.meta.evidence_mode 不一致"
            )
        transcript_segments = transcript.get("segments", [])
        transcript_id_counts = Counter(
            segment.get("id")
            for segment in transcript_segments
            if segment.get("id")
        )
        duplicate_transcript_ids = sorted(
            segment_id
            for segment_id, count in transcript_id_counts.items()
            if count > 1
        )
        if duplicate_transcript_ids:
            errors.append(
                f"transcript.raw.json 存在重复 segment id: "
                f"{duplicate_transcript_ids}")
        stale_segment_hashes = [
            segment.get("id")
            for segment in transcript_segments
            if segment.get("content_sha256")
            and segment.get("content_sha256") != hashlib.sha256(
                (segment.get("text") or "").strip().encode("utf-8")
            ).hexdigest()
        ]
        if stale_segment_hashes:
            errors.append(
                "transcript.raw.json 的 segment content_sha256 已过期: "
                f"{stale_segment_hashes}")
        transcript_ids = {
            segment.get("id") for segment in transcript_segments
            if segment.get("id")
        }
        synthetic_transcript_ids = {
            segment.get("id") for segment in transcript_segments
            if segment.get("id") and segment.get("synthetic_boundary")
        }
        ends = [
            segment.get("end")
            for segment in transcript_segments
            if segment.get("end") is not None
        ]
        transcript_max_end = max(ends) if ends else None
    for index, unit in enumerate(units):
        prefix = f"units[{index}]"
        if not isinstance(unit, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        uid = unit.get("id")
        display_id = uid or prefix
        if not uid or not re.fullmatch(r"U\d{4,}", str(uid)):
            errors.append(f"{prefix}.id 无效: {uid!r}")
        elif uid in ids:
            errors.append(f"重复的 unit id: {uid}")
        ids.add(uid)
        importance = unit.get("importance")
        status = unit.get("status")
        if importance not in IMPORTANCE_VALUES:
            errors.append(f"{display_id}: importance 必须是 {sorted(IMPORTANCE_VALUES)}")
        if status not in STATUS_VALUES:
            errors.append(f"{display_id}: status 必须是 {sorted(STATUS_VALUES)}")
        if status in {"pending", "needs_review", "unresolved"}:
            errors.append(f"{display_id}: status={status}，尚未完成核验")
        if not unit.get("topic"):
            errors.append(f"{display_id}: 缺少 topic")
        if importance in {"high", "medium"} and not unit.get("claims"):
            errors.append(f"{display_id}: {importance} 单元没有 claims")
        modalities = unit.get("claim_modalities")
        if modalities:
            if not isinstance(modalities, list):
                errors.append(f"{display_id}: claim_modalities 必须是数组")
            elif len(modalities) != len(unit.get("claims", [])):
                errors.append(
                    f"{display_id}: claim_modalities 数量必须与 claims 一致")
            else:
                invalid_modalities = sorted(set(modalities) - CLAIM_MODALITIES)
                if invalid_modalities:
                    errors.append(
                        f"{display_id}: claim modality 无效: {invalid_modalities}")
        if payload.get("detail_items_version") == DETAIL_ITEMS_VERSION:
            for text_field, item_field, item_prefix in (
                    ("numbers", "number_items", "N"),
                    ("examples", "example_items", "E")):
                texts = [str(value) for value in unit.get(text_field, []) or []]
                items = unit.get(item_field)
                expected_items = [
                    {"id": f"{item_prefix}{item_index:02d}", "text": text}
                    for item_index, text in enumerate(texts, start=1)
                ]
                if items != expected_items:
                    errors.append(
                        f"{display_id}: {item_field} 必须与 {text_field} "
                        "按顺序确定性对应")
        if evidence_mode == "timestamp":
            if not unit.get("timestamps"):
                errors.append(f"{display_id}: 缺少 timestamps，无法回溯原音频")
            for timestamp in unit.get("timestamps", []):
                if not isinstance(timestamp, list) or len(timestamp) != 2:
                    errors.append(
                        f"{display_id}: timestamp 格式无效: {timestamp!r}")
                    continue
                start, end = timestamp
                if start is None or end is None or start < 0 or end < start:
                    errors.append(
                        f"{display_id}: timestamp 范围无效: {timestamp!r}")
                elif transcript_max_end is not None and end > transcript_max_end + 1:
                    errors.append(
                        f"{display_id}: timestamp 超出转录范围: {timestamp!r}")
        elif unit.get("timestamps"):
            errors.append(
                f"{display_id}: text_anchor 模式不允许携带 timestamps")
        if status == "excluded":
            if not unit.get("notes"):
                errors.append(f"{display_id}: excluded 单元必须填写删除原因")
            if payload.get("source_accountability_version", 0) >= 1:
                exclusion_type = unit.get("exclusion_type")
                if exclusion_type not in EXCLUSION_TYPES:
                    errors.append(
                        f"{display_id}: excluded 单元 exclusion_type 必须是 "
                        f"{sorted(EXCLUSION_TYPES)}")
                if exclusion_type == "other" and len(
                        re.sub(r"\s+", "", str(unit.get("notes", "")))) < 10:
                    errors.append(f"{display_id}: exclusion_type=other 需要具体说明")
        if status == "unsupported":
            errors.append(f"{display_id}: 存在 unsupported 内容单元")
        if payload.get("schema_version", 1) >= CONTENT_MAP_SCHEMA_VERSION:
            evidence = unit.get("evidence")
            if not isinstance(evidence, dict):
                errors.append(f"{display_id}: 缺少 evidence")
                continue
            segment_ids = evidence.get("segment_ids")
            if not isinstance(segment_ids, list) or not segment_ids:
                errors.append(f"{display_id}: evidence.segment_ids 不能为空")
                continue
            evidence_mode_for_unit = evidence.get("mode", evidence_mode)
            if evidence_mode_for_unit != evidence_mode:
                errors.append(
                    f"{display_id}: evidence.mode 与 content_map.evidence_mode 不一致"
                )
            if len(segment_ids) > 60:
                warnings.append(
                    f"{display_id}: 单元证据包含 {len(segment_ids)} 个片段，"
                    "建议拆分 unit 以降低审查延迟并提高 claim 精度")
            if (
                    set(segment_ids) & synthetic_transcript_ids
                    and not synthetic_warning_added):
                warnings.append(
                    f"{display_id}: 证据引用无时间戳文本的合成分段，"
                    "segment 边界仅用于字符级定位")
                synthetic_warning_added = True
            unknown_segments = sorted(set(segment_ids) - transcript_ids)
            if transcript is None:
                errors.append(
                    f"{display_id}: segment evidence 校验需要 transcript.raw.json")
            elif unknown_segments:
                errors.append(
                    f"{display_id}: evidence 引用了未知片段: {unknown_segments}")
            else:
                expected_hash = segment_evidence_sha256(
                    transcript_segments, segment_ids)
                if evidence.get("source_sha256") != expected_hash:
                    errors.append(f"{display_id}: evidence.source_sha256 已过期")
            claim_evidence = unit.get("claim_evidence")
            if not isinstance(claim_evidence, dict):
                errors.append(f"{display_id}: 缺少 claim_evidence")
            else:
                expected_claim_keys = {
                    f"C{claim_index:02d}"
                    for claim_index, _claim in enumerate(
                        unit.get("claims", []), start=1)
                }
                extra_claim_keys = sorted(
                    set(claim_evidence) - expected_claim_keys)
                if extra_claim_keys:
                    errors.append(
                        f"{display_id}: 存在未知 claim 证据: "
                        f"{extra_claim_keys}")
                claim_sets = []
                for claim_index, _claim in enumerate(
                        unit.get("claims", []), start=1):
                    claim_key = f"C{claim_index:02d}"
                    claim_segments = claim_evidence.get(claim_key)
                    if not isinstance(claim_segments, list) or not claim_segments:
                        errors.append(
                            f"{display_id}-{claim_key}: 缺少证据片段")
                    elif not set(claim_segments).issubset(set(segment_ids)):
                        errors.append(
                            f"{display_id}-{claim_key}: claim 证据不属于单元证据")
                    else:
                        if len(claim_segments) != len(set(claim_segments)):
                            errors.append(
                                f"{display_id}-{claim_key}: claim 证据存在重复片段")
                        claim_sets.append(tuple(claim_segments))
                        if payload.get(
                                "schema_version", 1) >= CONTENT_MAP_SCHEMA_VERSION:
                            claim_hashes = unit.get(
                                "claim_evidence_sha256", {})
                            expected_claim_hash = segment_evidence_sha256(
                                transcript_segments, claim_segments)
                            if claim_hashes.get(
                                    claim_key) != expected_claim_hash:
                                errors.append(
                                    f"{display_id}-{claim_key}: "
                                    "claim evidence hash 已过期")
                            claim_notes = unit.get(
                                "claim_evidence_notes", {})
                            note = claim_notes.get(claim_key, {})
                            confidence = (
                                note.get("confidence")
                                if isinstance(note, dict) else None
                            )
                            rationale = (
                                str(note.get("rationale", "")).strip()
                                if isinstance(note, dict) else ""
                            )
                            if confidence not in CLAIM_CONFIDENCE_VALUES:
                                errors.append(
                                    f"{display_id}-{claim_key}: "
                                    "缺少有效 claim evidence confidence")
                            elif confidence == "low":
                                errors.append(
                                    f"{display_id}-{claim_key}: "
                                    "claim evidence confidence=low，需人工复核")
                            if not rationale:
                                errors.append(
                                    f"{display_id}-{claim_key}: "
                                    "缺少 claim evidence rationale")
                            elif len(re.sub(r"\s+", "", rationale)) < 10:
                                errors.append(
                                    f"{display_id}-{claim_key}: "
                                    "claim evidence rationale 过短，"
                                    "需说明片段如何支持 claim")
                if (
                        payload.get(
                            "schema_version", 1) >= CONTENT_MAP_SCHEMA_VERSION
                        and len(claim_sets) > 1
                        and len(segment_ids) >= 2
                        and sum(
                            set(claim_set) == set(segment_ids)
                            for claim_set in claim_sets) >= 2
                ):
                    errors.append(
                        f"{display_id}: 至少两条 claim 全量复用整个单元证据，"
                        "必须收窄到 claim 级最小片段")

    if (
            transcript is not None
            and payload.get("source_accountability_version", 0) >= 1):
        accountability = source_segment_accountability(payload, transcript)
        if accountability["missing_ids"]:
            errors.append(
                "content map 缺少源 segment: "
                f"{accountability['missing_ids']}")
        if accountability["duplicate_ids"]:
            warnings.append(
                "content map 重复引用源 segment: "
                f"{accountability['duplicate_ids']}")
        if accountability["invalid_unit_ids"]:
            errors.append(
                "content map 存在未完成或无效分类单元: "
                f"{accountability['invalid_unit_ids']}")

    return errors, warnings


def normalize_detail_items(content_map):
    """Derive stable number/example IDs while retaining legacy text arrays."""
    for unit in content_map.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        unit["number_items"] = [
            {"id": f"N{index:02d}", "text": str(text)}
            for index, text in enumerate(unit.get("numbers", []) or [], start=1)
        ]
        unit["example_items"] = [
            {"id": f"E{index:02d}", "text": str(text)}
            for index, text in enumerate(unit.get("examples", []) or [], start=1)
        ]
    content_map["detail_items_version"] = DETAIL_ITEMS_VERSION
    return content_map


def unit_detail_ids(content_map, detail_type, purpose="notes"):
    if detail_type not in {"number", "example"}:
        raise ValueError(f"未知 detail type: {detail_type}")
    if purpose != "notes":
        raise ValueError(f"未知 detail coverage purpose: {purpose}")
    field = "number_items" if detail_type == "number" else "example_items"
    prefix = "N" if detail_type == "number" else "E"
    result = []
    for unit in content_map.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        if unit.get("status") not in {"included", "condensed"}:
            continue
        if unit.get("importance") not in {"high", "medium"}:
            continue
        unit_id = unit.get("id")
        for index, item in enumerate(unit.get(field, []) or [], start=1):
            item_id = item.get("id") if isinstance(item, dict) else None
            result.append(f"{unit_id}-{item_id or f'{prefix}{index:02d}'}")
    return result


def load_summary_map(path):
    payload = load_json(path)
    if isinstance(payload, list):
        return {"chapters": payload}
    return payload


def unit_claim_ids(content_map, include_excluded=False, purpose="notes"):
    """Return claim IDs required or allowed for a downstream artifact."""
    if purpose not in {"notes", "briefing_required", "briefing_allowed"}:
        raise ValueError(f"未知 claim coverage purpose: {purpose}")
    result = []
    for unit in content_map.get("units", []):
        status = unit.get("status")
        importance = unit.get("importance")
        if status == "excluded" and not include_excluded:
            continue
        if purpose == "notes":
            selected = (
                status == "condensed"
                or importance in {"high", "medium"}
            )
        elif purpose == "briefing_required":
            selected = (
                status == "included"
                and importance in {"high", "medium"}
            )
        else:
            selected = status in {"included", "condensed"}
        if not selected:
            continue
        for index, _claim in enumerate(unit.get("claims", []), start=1):
            result.append(f"{unit.get('id')}-C{index:02d}")
    return result


def briefing_chapters(text):
    """Return chapter bodies using the canonical Markdown section parser."""
    return chapter_body_map(text)


def body_sha256(body):
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()


def validate_summary_map(
        payload, briefing_text=None, content_map=None, notes_text=None):
    """校验 summary_map 结构，并可与讲书稿的 ## 标题对齐。"""
    errors = []
    if not isinstance(payload, dict) or not isinstance(payload.get("chapters"), list):
        return ["summary_map.chapters 必须是数组"]
    chapters = payload["chapters"]
    if not chapters:
        errors.append("summary_map.chapters 不能为空")
    seen_titles = set()
    seen_units = set()
    seen_claims = set()
    actual_chapters = briefing_chapters(briefing_text) if briefing_text is not None else {}
    for index, chapter in enumerate(chapters):
        prefix = f"chapters[{index}]"
        if not isinstance(chapter, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        title = str(chapter.get("title", "")).strip()
        unit_ids = chapter.get("unit_ids")
        claim_ids = chapter.get("claim_ids")
        if not title:
            errors.append(f"{prefix}.title 不能为空")
        if title in seen_titles:
            errors.append(f"重复的章节标题: {title}")
        seen_titles.add(title)
        if not isinstance(unit_ids, list) or not unit_ids:
            errors.append(f"{title or prefix}.unit_ids 不能为空")
            continue
        for uid in unit_ids:
            if uid in seen_units:
                errors.append(f"内容单元重复映射到多个章节: {uid}")
            seen_units.add(uid)
        if content_map is not None:
            if not isinstance(claim_ids, list) or not claim_ids:
                errors.append(f"{title or prefix}.claim_ids 不能为空")
            else:
                for claim_id in claim_ids:
                    if claim_id in seen_claims:
                        errors.append(f"claim 重复映射到多个章节: {claim_id}")
                    seen_claims.add(claim_id)
        if briefing_text is not None and title in actual_chapters:
            expected_hash = body_sha256(actual_chapters[title])
            if chapter.get("body_sha256") != expected_hash:
                errors.append(f"{title}: body_sha256 与当前讲稿正文不一致")

    if briefing_text is not None:
        actual_titles = set(actual_chapters)
        mapped_titles = seen_titles - {""}
        missing = sorted(actual_titles - mapped_titles)
        extra = sorted(mapped_titles - actual_titles)
        if missing:
            errors.append(f"讲稿存在未映射章节: {missing}")
        if extra:
            errors.append(f"summary_map 存在讲稿中不存在的章节: {extra}")
    excluded_unit_ids = {
        str(unit.get("id"))
        for unit in (content_map or {}).get("units", [])
        if isinstance(unit, dict) and unit.get("status") == "excluded"
    }
    excluded_references = sorted(seen_units & excluded_unit_ids)
    if excluded_references:
        errors.append(
            f"summary_map 不得引用 excluded 单元: {excluded_references}")
    if (
            content_map is not None
            and payload.get("schema_version", 1) >= SUMMARY_MAP_SCHEMA_VERSION):
        expected_claims = set(unit_claim_ids(content_map, purpose="notes"))
        notes_claim_ids = payload.get("notes_claim_ids")
        if not isinstance(notes_claim_ids, list):
            errors.append("summary_map.notes_claim_ids 必须是数组")
        else:
            notes_claim_set = set(notes_claim_ids)
            missing_notes_claims = sorted(expected_claims - notes_claim_set)
            unknown_notes_claims = sorted(notes_claim_set - expected_claims)
            if missing_notes_claims:
                errors.append(
                    f"完整笔记缺少 claim 映射: {missing_notes_claims}")
            if unknown_notes_claims:
                errors.append(
                    f"完整笔记包含未知 claim 映射: {unknown_notes_claims}")
        if notes_text is None:
            errors.append("v2 summary_map 校验需要 中文完整笔记.md")
        elif payload.get("notes_sha256") != body_sha256(notes_text):
            errors.append("summary_map.notes_sha256 与当前完整笔记不一致")
        if content_map.get("detail_items_version") == DETAIL_ITEMS_VERSION:
            for field, detail_type, label in (
                    ("notes_number_ids", "number", "数字"),
                    ("notes_example_ids", "example", "例子")):
                expected_details = set(unit_detail_ids(
                    content_map, detail_type, purpose="notes"))
                values = payload.get(field)
                if not isinstance(values, list):
                    errors.append(f"summary_map.{field} 必须是数组")
                    continue
                actual_details = set(values)
                missing_details = sorted(expected_details - actual_details)
                unknown_details = sorted(actual_details - expected_details)
                if missing_details:
                    errors.append(f"完整笔记缺少{label}映射: {missing_details}")
                if unknown_details:
                    errors.append(f"完整笔记包含未知{label}映射: {unknown_details}")
    return errors


def coverage_report(content_map, summary_map, transcript=None):
    units = content_map.get("units", [])
    chapters = summary_map.get("chapters", [])
    unit_by_id = {unit.get("id"): unit for unit in units}
    referenced = []
    for chapter in chapters:
        for uid in chapter.get("unit_ids", []):
            referenced.append(uid)

    referenced_set = set(referenced)
    referenced_claims = []
    for chapter in chapters:
        if isinstance(chapter, dict):
            referenced_claims.extend(chapter.get("claim_ids", []) or [])
    referenced_claim_set = set(referenced_claims)
    required_briefing_claims = set(unit_claim_ids(
        content_map, purpose="briefing_required"))
    allowed_briefing_claims = set(unit_claim_ids(
        content_map, purpose="briefing_allowed"))
    expected_notes_claims = set(unit_claim_ids(content_map, purpose="notes"))
    unknown_claims = sorted(referenced_claim_set - allowed_briefing_claims)
    missing_claims = sorted(required_briefing_claims - referenced_claim_set)
    duplicate_claims = sorted(
        claim_id for claim_id in referenced_claim_set
        if referenced_claims.count(claim_id) > 1
    )
    high = {
        u["id"] for u in units
        if u.get("importance") == "high" and u.get("status") == "included"
    }
    medium = {
        u["id"] for u in units
        if u.get("importance") == "medium" and u.get("status") == "included"
    }
    unsupported = {u["id"] for u in units if u.get("status") == "unsupported"}

    required_high = high
    required_medium = medium
    unknown = sorted(referenced_set - set(unit_by_id))
    high_missing = sorted(required_high - referenced_set)
    medium_missing = sorted(required_medium - referenced_set)
    duplicate_refs = sorted(uid for uid in set(referenced) if referenced.count(uid) > 1)
    explicit_exclusion_missing_reason = sorted(
        u["id"] for u in units
        if u.get("status") == "excluded" and not u.get("notes")
    )
    notes_claims = set(summary_map.get("notes_claim_ids", []) or [])
    notes_missing_claims = sorted(expected_notes_claims - notes_claims)
    notes_unknown_claims = sorted(notes_claims - expected_notes_claims)
    expected_number_ids = (
        set(unit_detail_ids(content_map, "number", purpose="notes"))
        if content_map.get("detail_items_version") == DETAIL_ITEMS_VERSION
        else set()
    )
    expected_example_ids = (
        set(unit_detail_ids(content_map, "example", purpose="notes"))
        if content_map.get("detail_items_version") == DETAIL_ITEMS_VERSION
        else set()
    )
    notes_number_ids = set(summary_map.get("notes_number_ids", []) or [])
    notes_example_ids = set(summary_map.get("notes_example_ids", []) or [])
    missing_number_ids = sorted(expected_number_ids - notes_number_ids)
    unknown_number_ids = sorted(notes_number_ids - expected_number_ids)
    missing_example_ids = sorted(expected_example_ids - notes_example_ids)
    unknown_example_ids = sorted(notes_example_ids - expected_example_ids)

    accountability = (
        source_segment_accountability(content_map, transcript)
        if transcript is not None else None
    )
    return {
        "chapter_count": len(chapters),
        "unit_count": len(units),
        "referenced_unit_count": len(referenced_set),
        "high_total": len(required_high),
        "high_covered": len(required_high & referenced_set),
        "high_coverage": round(
            len(required_high & referenced_set) / len(required_high), 4
        ) if required_high else 1.0,
        "medium_total": len(required_medium),
        "medium_covered": len(required_medium & referenced_set),
        "medium_coverage": round(
            len(required_medium & referenced_set) / len(required_medium), 4
        ) if required_medium else 1.0,
        "unknown_unit_ids": unknown,
        "high_missing": high_missing,
        "medium_missing": medium_missing,
        "duplicate_references": duplicate_refs,
        "claim_total": len(required_briefing_claims),
        "claim_covered": len(required_briefing_claims & referenced_claim_set),
        "claim_coverage": round(
            len(required_briefing_claims & referenced_claim_set)
            / len(required_briefing_claims), 4
        ) if required_briefing_claims else 1.0,
        "unknown_claim_ids": unknown_claims,
        "missing_claim_ids": missing_claims,
        "duplicate_claim_ids": duplicate_claims,
        "notes_claim_total": len(expected_notes_claims),
        "notes_claim_covered": len(expected_notes_claims & notes_claims),
        "notes_claim_coverage": round(
            len(expected_notes_claims & notes_claims) / len(expected_notes_claims), 4
        ) if expected_notes_claims else 1.0,
        "notes_missing_claim_ids": notes_missing_claims,
        "notes_unknown_claim_ids": notes_unknown_claims,
        "notes_number_total": len(expected_number_ids),
        "notes_number_covered": len(expected_number_ids & notes_number_ids),
        "notes_number_coverage": round(
            len(expected_number_ids & notes_number_ids) / len(expected_number_ids), 4
        ) if expected_number_ids else 1.0,
        "notes_missing_number_ids": missing_number_ids,
        "notes_unknown_number_ids": unknown_number_ids,
        "notes_example_total": len(expected_example_ids),
        "notes_example_covered": len(expected_example_ids & notes_example_ids),
        "notes_example_coverage": round(
            len(expected_example_ids & notes_example_ids) / len(expected_example_ids), 4
        ) if expected_example_ids else 1.0,
        "notes_missing_example_ids": missing_example_ids,
        "notes_unknown_example_ids": unknown_example_ids,
        "unsupported_units": sorted(unsupported),
        "excluded_without_reason": explicit_exclusion_missing_reason,
        "source_segment_coverage": accountability,
        "passed": not (
            unknown or high_missing or medium_missing or unknown_claims
            or missing_claims or duplicate_claims or unsupported
            or explicit_exclusion_missing_reason
            or (
                summary_map.get("schema_version", 1) >= SUMMARY_MAP_SCHEMA_VERSION
                and (
                    notes_missing_claims or notes_unknown_claims
                    or missing_number_ids or unknown_number_ids
                    or missing_example_ids or unknown_example_ids
                )
            )
            or (accountability is not None and not accountability["passed"])
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="内容单元台账和总结覆盖率工具")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="从 transcript.raw.json 创建内容台账模板")
    init.add_argument("transcript_json")
    init.add_argument("output")
    init.add_argument("--title", default="")

    check = sub.add_parser("check", help="校验 content_map.json")
    check.add_argument("content_map")
    check.add_argument("--transcript", default=None)

    report = sub.add_parser("coverage", help="检查总结对内容单元的覆盖率")
    report.add_argument("content_map")
    report.add_argument("summary_map")
    report.add_argument("--out", default=None)

    enrich = sub.add_parser(
        "enrich-evidence",
        help="为单集补齐 v3 segment/claim/notes 证据绑定",
    )
    enrich.add_argument("folder")

    args = parser.parse_args()
    if args.command == "init":
        payload = init_content_map(args.transcript_json, args.output, args.title)
        print(f"[content-map] 已创建 {len(payload['units'])} 个待整理片段 → {args.output}")
        return 0
    if args.command == "check":
        transcript = load_json(args.transcript) if args.transcript else None
        errors, warnings = validate_content_map(
            load_json(args.content_map), transcript)
        for item in errors:
            print(f"[错误] {item}")
        for item in warnings:
            print(f"[警告] {item}")
        return 1 if errors else 0
    if args.command == "coverage":
        result = coverage_report(load_json(args.content_map), load_summary_map(args.summary_map))
        if args.out:
            save_json(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if args.command == "enrich-evidence":
        folder = Path(args.folder)
        transcript_path = folder / "transcript.raw.json"
        content_map_path = folder / "content_map.json"
        summary_map_path = folder / "summary_map.json"
        notes_path = folder / "中文完整笔记.md"
        required = [
            transcript_path, content_map_path, summary_map_path, notes_path]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            print(f"[错误] 缺少证据迁移文件: {missing}")
            return 1
        content_map, transcript = enrich_content_map_evidence(
            load_json(content_map_path), load_json(transcript_path))
        missing_segment_ids = [
            index
            for index, segment in enumerate(
                load_json(transcript_path).get("segments", []), start=1)
            if not segment.get("id")
        ]
        if missing_segment_ids:
            print(
                "[错误] transcript.raw.json 缺少持久化 segment ID，"
                "拒绝由迁移命令改写原始证据"
            )
            return 1
        summary_map = enrich_summary_map_evidence(
            load_summary_map(summary_map_path),
            notes_path.read_text(encoding="utf-8"),
            content_map,
            (folder / "讲书稿.md").read_text(encoding="utf-8"),
        )
        basis_path = (
            folder / "转录_纠错.txt"
            if (folder / "转录_纠错.txt").exists()
            else folder / "原始转录.txt"
        )
        if basis_path.exists():
            summary_map["transcript_basis"] = {
                "file": basis_path.name,
                "sha256": body_sha256(
                    basis_path.read_text(encoding="utf-8")),
            }
        save_json(content_map_path, content_map)
        save_json(summary_map_path, summary_map)
        errors, warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map,
            (folder / "讲书稿.md").read_text(encoding="utf-8"),
            content_map,
            notes_path.read_text(encoding="utf-8"),
        )
        for item in errors + summary_errors:
            print(f"[错误] {item}")
        for item in warnings:
            print(f"[警告] {item}")
        if errors or summary_errors:
            return 1
        print(
            f"[content-map] v3 evidence 已补齐: "
            f"{len(content_map.get('units', []))} units"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

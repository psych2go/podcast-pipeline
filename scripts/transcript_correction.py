"""Revision-bound, segment-complete transcript correction artifacts."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

try:
    from atomic_io import atomic_write_json, atomic_write_text
    from hashing import sha256_text
    from transcript_completeness import parse_contract_version
except ImportError:
    from scripts.atomic_io import atomic_write_json, atomic_write_text
    from scripts.hashing import sha256_text
    from scripts.transcript_completeness import parse_contract_version


CORRECTION_SCHEMA_VERSION = 1
CORRECTION_CONTRACT_VERSION = 1
MANIFEST_NAME = "correction_manifest.json"
CORRECTED_NAME = "转录_纠错.txt"
VERIFICATION_VALUES = {
    "not_required", "context_only", "alternate_decode",
    "external_entity_source", "human_audio", "unresolved",
}
STATUS_VALUES = {"unchanged", "corrected", "unresolved"}
REQUIRED_ITEM_FIELDS = {
    "segment_id", "corrected_text", "status", "change_types",
    "verification", "unresolved",
}
HIGH_RISK_RE = re.compile(
    r"(?:\$|€|£|¥|\b\d+(?:[,.]\d+)*(?:%|x|k|m|b)?\b|"
    r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*)+\b)"
)
SPEAKER_LABEL_RE = re.compile(
    r"^\s*(?:\[(?:speaker[^\]]*|host|guest)\]|"
    r"(?:speaker(?:_\d+)?|host|guest)\s*:)\s*",
    re.IGNORECASE,
)


class CorrectionValidationError(RuntimeError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:10]))


def correction_contract_required(raw):
    meta = raw.get("meta", {}) or {}
    return parse_contract_version(
        meta.get("correction_contract_version"),
        field="correction_contract_version",
    ) >= CORRECTION_CONTRACT_VERSION


def correction_batches(segments, max_chars=30000):
    """Return consecutive nonempty segment batches without splitting segments."""
    batches, current, current_chars = [], [], 0
    for segment in segments:
        if not (segment.get("text") or "").strip():
            continue
        size = len(segment.get("text", ""))
        if current and current_chars + size > max_chars:
            batches.append(current)
            current, current_chars = [], 0
        current.append(segment)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _word_count(text):
    return len(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", text or "", re.UNICODE))


def _source_segments(raw):
    return [
        segment for segment in raw.get("segments", [])
        if isinstance(segment, dict) and (segment.get("text") or "").strip()
    ]


def _source_text(segment):
    return str(segment.get("text", "")).strip()


def _text_similarity(source, corrected):
    return SequenceMatcher(
        None,
        re.sub(r"\s+", " ", source).lower(),
        re.sub(r"\s+", " ", corrected).lower(),
        autojunk=False,
    ).ratio()


def _validate_item(item, source_segment, *, require_source_hash=False):
    segment_id = str(source_segment.get("id"))
    errors = []
    if not isinstance(item, dict):
        return [f"{segment_id}: correction item 必须是对象"]
    missing = sorted(REQUIRED_ITEM_FIELDS - set(item))
    if missing:
        errors.append(f"{segment_id}: correction item 缺少字段 {missing}")
    if str(item.get("segment_id")) != segment_id:
        errors.append(f"{segment_id}: segment_id 不匹配")
    if not isinstance(item.get("corrected_text"), str):
        errors.append(f"{segment_id}: corrected_text 必须是字符串")
        corrected_text = ""
    else:
        corrected_text = item["corrected_text"].strip()
    if not corrected_text:
        errors.append(f"{segment_id}: corrected_text 不能为空")
    if "\n" in corrected_text or "\r" in corrected_text:
        errors.append(f"{segment_id}: corrected_text 必须是单行文本")
    if SPEAKER_LABEL_RE.match(corrected_text):
        errors.append(f"{segment_id}: corrected_text 不得自行写 speaker 标签")

    status = item.get("status")
    verification = item.get("verification")
    change_types = item.get("change_types")
    unresolved = item.get("unresolved")
    if status not in STATUS_VALUES:
        errors.append(f"{segment_id}: correction status 无效")
    if verification not in VERIFICATION_VALUES:
        errors.append(f"{segment_id}: verification 无效")
    if not isinstance(change_types, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in change_types):
        errors.append(f"{segment_id}: change_types 必须是非空字符串数组")
        change_types = []
    if not isinstance(unresolved, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in unresolved):
        errors.append(f"{segment_id}: unresolved 必须是非空字符串数组")
        unresolved = []

    source_text = _source_text(source_segment)
    changed = corrected_text != source_text
    flagged = bool(
        source_segment.get("needs_redecode")
        or source_segment.get("needs_review")
        or source_segment.get("speaker_alignment") == "unresolved"
    )
    if status == "unchanged":
        if changed:
            errors.append(f"{segment_id}: unchanged 状态不得改变文本")
        if change_types:
            errors.append(f"{segment_id}: unchanged 状态不得填写 change_types")
        if verification != "not_required" or unresolved:
            errors.append(f"{segment_id}: unchanged 状态验证字段不一致")
        if flagged:
            errors.append(
                f"{segment_id}: 已标记待复核的 segment "
                "不得声明 unchanged/not_required")
    elif status == "corrected":
        if not changed:
            errors.append(f"{segment_id}: corrected 状态必须实际改变文本")
        if not change_types:
            errors.append(f"{segment_id}: corrected 状态必须填写 change_types")
        if verification in {"not_required", "unresolved", None}:
            errors.append(f"{segment_id}: corrected 状态 verification 不充分")
        if unresolved:
            errors.append(f"{segment_id}: corrected 状态不得保留 unresolved")
        if (
                HIGH_RISK_RE.search(source_text + " " + corrected_text)
                and verification not in {
                    "alternate_decode", "external_entity_source", "human_audio"
                }):
            errors.append(f"{segment_id}: 高风险纠错缺少独立验证")
        if verification == "alternate_decode" and not source_segment.get(
                "refinement"):
            errors.append(f"{segment_id}: alternate_decode 缺少 refinement 证据")
        source_words = max(1, _word_count(source_text))
        corrected_words = max(1, _word_count(corrected_text))
        length_ratio = corrected_words / source_words
        similarity = _text_similarity(source_text, corrected_text)
        if (
                (length_ratio < 0.6 or length_ratio > 1.6 or similarity < 0.55)
                and verification != "human_audio"):
            errors.append(
                f"{segment_id}: 纠错改写幅度过大，必须保留原文为 unresolved "
                "或提供 human_audio 验证")
    elif status == "unresolved":
        if changed:
            errors.append(f"{segment_id}: unresolved 状态必须保留源文本")
        if verification != "unresolved" or not unresolved:
            errors.append(f"{segment_id}: unresolved 状态字段不完整")
        for value in unresolved:
            if HIGH_RISK_RE.search(value):
                errors.append(f"{segment_id}: 高风险数字或实体仍未解决: {value}")

    if require_source_hash:
        expected_hash = source_segment.get("content_sha256") or sha256_text(source_text)
        if item.get("source_sha256") != expected_hash:
            errors.append(f"{segment_id}: source_sha256 不匹配")
    return errors


def validate_correction_batch(raw, items, expected_ids=None):
    source = _source_segments(raw)
    source_by_id = {str(segment.get("id")): segment for segment in source}
    errors = []
    if not isinstance(items, list):
        return ["correction batch segments 必须是数组"]
    actual_ids = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"correction batch segments[{index}] 必须是对象")
            continue
        segment_id = str(item.get("segment_id"))
        actual_ids.append(segment_id)
        source_segment = source_by_id.get(segment_id)
        if source_segment is None:
            errors.append(f"correction batch 引用了未知 segment: {segment_id}")
            continue
        errors.extend(_validate_item(item, source_segment))
    if expected_ids is not None and actual_ids != list(expected_ids):
        errors.append(
            "correction batch segment 覆盖或顺序不匹配: "
            f"expected={list(expected_ids)}, actual={actual_ids}")
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("correction batch 存在重复 segment ID")
    return errors


def build_manifest(raw, corrected_items):
    source = _source_segments(raw)
    expected_ids = [str(segment.get("id")) for segment in source]
    errors = validate_correction_batch(raw, corrected_items, expected_ids)
    if errors:
        raise CorrectionValidationError(errors)
    source_by_id = {str(segment.get("id")): segment for segment in source}
    items = []
    for original in corrected_items:
        item = dict(original)
        segment_id = str(item["segment_id"])
        segment = source_by_id[segment_id]
        item["source_sha256"] = segment.get("content_sha256") or sha256_text(
            _source_text(segment))
        items.append(item)
    manifest = {
        "schema_version": CORRECTION_SCHEMA_VERSION,
        "evidence_revision_id": raw.get("evidence", {}).get("revision_id", ""),
        "source_transcript_sha256": raw.get("evidence", {}).get(
            "transcript_sha256", ""),
        "segments": items,
    }
    corrected = render_corrected_transcript(raw, manifest)
    manifest["corrected_transcript_sha256"] = sha256_text(corrected)
    manifest["summary"] = correction_summary(raw, manifest)
    return manifest


def correction_summary(raw, manifest):
    source = _source_segments(raw)
    corrected = [
        item for item in manifest.get("segments", [])
        if isinstance(item, dict)
    ] if isinstance(manifest, dict) else []
    source_words = sum(_word_count(_source_text(item)) for item in source)
    corrected_words = sum(
        _word_count(item.get("corrected_text", "")) for item in corrected)
    return {
        "total_segments": len(source),
        "unchanged": sum(item.get("status") == "unchanged" for item in corrected),
        "corrected": sum(item.get("status") == "corrected" for item in corrected),
        "unresolved": sum(item.get("status") == "unresolved" for item in corrected),
        "source_words": source_words,
        "corrected_words": corrected_words,
        "word_retention_ratio": round(
            corrected_words / source_words, 6) if source_words else 1.0,
    }


def render_corrected_transcript(raw, manifest):
    source_by_id = {
        str(segment.get("id")): segment for segment in _source_segments(raw)
    }
    lines = []
    items = manifest.get("segments", []) if isinstance(manifest, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("corrected_text", "")).strip()
        source = source_by_id.get(str(item.get("segment_id")), {})
        speaker = source.get("speaker")
        prefix = f"[{speaker}] " if speaker else ""
        lines.append(prefix + text)
    return "\n".join(lines).strip()


def validate_correction_manifest(
        raw, manifest, *, max_deletion_ratio=0.02,
        rendered_text=None):
    errors = []
    if not isinstance(manifest, dict):
        return ["correction manifest 必须是对象"]
    if manifest.get("schema_version") != CORRECTION_SCHEMA_VERSION:
        errors.append("correction manifest schema 不受支持")
    evidence = raw.get("evidence", {}) or {}
    if manifest.get("evidence_revision_id") != evidence.get("revision_id"):
        errors.append("correction manifest evidence revision 不匹配")
    if manifest.get("source_transcript_sha256") != evidence.get("transcript_sha256"):
        errors.append("correction manifest 原始转录哈希不匹配")

    source = _source_segments(raw)
    expected_ids = [str(segment.get("id")) for segment in source]
    items = manifest.get("segments")
    if not isinstance(items, list):
        return errors + ["correction manifest segments 必须是数组"]
    actual_ids = [
        str(item.get("segment_id")) for item in items if isinstance(item, dict)
    ]
    if len(actual_ids) != len(items):
        errors.append("correction manifest segment 必须全部是对象")
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("correction manifest 存在重复 segment ID")
    if actual_ids != expected_ids:
        missing = [item for item in expected_ids if item not in set(actual_ids)]
        unknown = [item for item in actual_ids if item not in set(expected_ids)]
        errors.append(
            "correction manifest segment 顺序或覆盖不匹配; "
            f"missing={missing}, unknown={unknown}")

    source_by_id = {str(segment.get("id")): segment for segment in source}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"correction manifest segments[{index}] 必须是对象")
            continue
        segment_id = str(item.get("segment_id"))
        segment = source_by_id.get(segment_id)
        if segment is None:
            continue
        errors.extend(_validate_item(item, segment, require_source_hash=True))

    summary = correction_summary(raw, manifest)
    if manifest.get("summary") != summary:
        errors.append("correction manifest summary 与重新计算结果不一致")
    if summary["word_retention_ratio"] < 1.0 - max_deletion_ratio:
        errors.append(
            "纠错稿词数下降超过允许阈值: "
            f"{summary['word_retention_ratio']:.2%}")
    corrected = render_corrected_transcript(raw, manifest)
    expected_corrected_hash = manifest.get("corrected_transcript_sha256")
    if expected_corrected_hash != sha256_text(corrected):
        errors.append("correction manifest 纠错稿哈希不匹配")
    if rendered_text is not None:
        actual = str(rendered_text).strip()
        if actual != corrected or sha256_text(actual) != expected_corrected_hash:
            errors.append("转录_纠错.txt 与 correction manifest 不一致")
    return errors


def write_correction_artifacts(folder, raw, manifest):
    errors = validate_correction_manifest(raw, manifest)
    if errors:
        raise CorrectionValidationError(errors)
    folder = Path(folder)
    corrected = render_corrected_transcript(raw, manifest)
    atomic_write_json(folder / MANIFEST_NAME, manifest)
    atomic_write_text(folder / CORRECTED_NAME, corrected)
    return {
        "manifest": folder / MANIFEST_NAME,
        "corrected": folder / CORRECTED_NAME,
        "summary": correction_summary(raw, manifest),
    }


def batch_output_schema():
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_id": {"type": "string"},
                        "corrected_text": {"type": "string"},
                        "status": {"type": "string", "enum": sorted(STATUS_VALUES)},
                        "change_types": {"type": "array", "items": {"type": "string"}},
                        "verification": {"type": "string", "enum": sorted(VERIFICATION_VALUES)},
                        "unresolved": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }

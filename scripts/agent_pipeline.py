"""Subagent-orchestrated content production for a single episode."""
import json
import os
import re
from contextlib import nullcontext
from pathlib import Path

try:
    from atomic_io import atomic_write_json, atomic_write_text
    from canonical_entities import (
        GENERATION_SCHEMA as CANONICAL_ENTITIES_SCHEMA,
        SCHEMA_VERSION as CANONICAL_ENTITIES_VERSION,
        public_entity_alias_errors,
        validate_canonical_entities,
    )
    from claim_evidence import refine_claim_evidence
    from content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        normalize_detail_items,
        normalize_summary_claim_ids,
        save_json,
        CLAIM_MODALITIES,
        STATUS_VALUES,
        validate_content_map,
        validate_summary_map,
    )
    from content_finalizer import (
        finalize_content_package,
        validate_tts_readiness,
    )
    from episode import (
        quality_metadata,
        sync_episode_state,
        update_transcript_status,
    )
    from evidence import ASR_SOURCE_KINDS, effective_source_kind
    from subagent import run_edit_task, run_json_task
    from prewrite_fact_checks import (
        FILENAME as PREWRITE_FACT_CHECKS_FILENAME,
        SCHEMA_VERSION as PREWRITE_FACT_CHECKS_VERSION,
        ledger_is_current,
        run_prewrite_fact_checks,
    )
    from tts import load_tts_lexicon
    from transcript_correction import (
        MANIFEST_NAME as CORRECTION_MANIFEST_NAME,
        batch_output_schema,
        build_manifest,
        correction_batches,
        correction_contract_required,
        validate_correction_batch,
        validate_correction_manifest,
        write_correction_artifacts,
    )
    from transcript_completeness import (
        completeness_contract_required,
        completeness_enforcement_mode,
        validate_completeness_result,
    )
except ImportError:
    from scripts.atomic_io import atomic_write_json, atomic_write_text
    from scripts.canonical_entities import (
        GENERATION_SCHEMA as CANONICAL_ENTITIES_SCHEMA,
        SCHEMA_VERSION as CANONICAL_ENTITIES_VERSION,
        public_entity_alias_errors,
        validate_canonical_entities,
    )
    from scripts.claim_evidence import refine_claim_evidence
    from scripts.content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        normalize_detail_items,
        normalize_summary_claim_ids,
        save_json,
        CLAIM_MODALITIES,
        STATUS_VALUES,
        validate_content_map,
        validate_summary_map,
    )
    from scripts.content_finalizer import (
        finalize_content_package,
        validate_tts_readiness,
    )
    from scripts.episode import (
        quality_metadata,
        sync_episode_state,
        update_transcript_status,
    )
    from scripts.evidence import ASR_SOURCE_KINDS, effective_source_kind
    from scripts.subagent import run_edit_task, run_json_task
    from scripts.prewrite_fact_checks import (
        FILENAME as PREWRITE_FACT_CHECKS_FILENAME,
        SCHEMA_VERSION as PREWRITE_FACT_CHECKS_VERSION,
        ledger_is_current,
        run_prewrite_fact_checks,
    )
    from scripts.tts import load_tts_lexicon
    from scripts.transcript_correction import (
        MANIFEST_NAME as CORRECTION_MANIFEST_NAME,
        batch_output_schema,
        build_manifest,
        correction_batches,
        correction_contract_required,
        validate_correction_batch,
        validate_correction_manifest,
        write_correction_artifacts,
    )
    from scripts.transcript_completeness import (
        completeness_contract_required,
        completeness_enforcement_mode,
        validate_completeness_result,
    )


def _stage(report, name, metrics=None):
    return report.stage(name, metrics) if report is not None else nullcontext()


def _env_positive_int(name, default):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} 必须 >= 1，当前值: {value}")
    return value


def _transcript_basis(folder):
    corrected = folder / "转录_纠错.txt"
    path = corrected if corrected.exists() else folder / "原始转录.txt"
    return {
        "file": path.name,
        "sha256": body_sha256(path.read_text(encoding="utf-8")),
    }


def _transcript_basis_is_current(folder, summary_map):
    return summary_map.get("transcript_basis") == _transcript_basis(folder)


def _ensure_content_map(folder, title, force=False):
    path = folder / "content_map.json"
    if force or not path.exists():
        init_content_map(
            folder / "transcript.raw.json",
            path,
            title=title,
        )
    return path


def _validate_content_map_stage_statuses(payload):
    errors = []
    for index, unit in enumerate(payload.get("units", [])):
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("id") or f"units[{index}]"
        status = unit.get("status")
        if status not in STATUS_VALUES:
            errors.append(f"{unit_id}: 未知 status={status!r}")
        elif status not in {"included", "condensed", "excluded"}:
            errors.append(f"{unit_id}: status={status!r} 尚未完成")
        segment_ids = unit.get("evidence", {}).get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            errors.append(f"{unit_id}: evidence.segment_ids 不能为空")
        if status == "excluded" and unit.get("claims"):
            errors.append(f"{unit_id}: excluded 单元不得生成 claims")
    if errors:
        raise RuntimeError(
            "content_map 阶段状态校验失败: " + "; ".join(errors[:10]))


def _accepted_transcript_status(value):
    return str(value or "").startswith(("官方字幕", "可接受", "已纠错"))


def _content_map_is_valid(folder):
    path = folder / "content_map.json"
    raw_path = folder / "transcript.raw.json"
    if not path.exists() or not raw_path.exists():
        return False
    try:
        content_map = load_json(path)
        transcript = load_json(raw_path)
        errors, _warnings = validate_content_map(content_map, transcript)
        return content_map.get("schema_version", 1) >= 3 and not errors
    except (OSError, ValueError, TypeError):
        return False


def _canonical_entities_is_valid(folder, transcript):
    path = Path(folder) / "canonical_entities.json"
    if not path.exists():
        return False
    try:
        payload = load_json(path)
    except (OSError, ValueError, TypeError):
        return False
    return not validate_canonical_entities(payload, transcript)


def content_pipeline_needed(folder, force=False):
    """Return whether semantic content artifacts need generation or repair."""
    folder = Path(folder)
    if force:
        return True
    required = [
        folder / "transcript.raw.json",
        folder / "原始转录.txt",
        folder / "content_map.json",
        folder / "中文完整笔记.md",
        folder / "讲书稿.md",
        folder / "summary_map.json",
    ]
    if any(not path.exists() for path in required):
        return True
    if not _accepted_transcript_status(
            quality_metadata(folder).get("transcript_status")):
        return True
    try:
        transcript = load_json(folder / "transcript.raw.json")
        content_map = load_json(folder / "content_map.json")
        if content_map.get("schema_version", 1) < 3:
            return True
        if (
                content_map.get("canonical_entities_contract_version")
                == CANONICAL_ENTITIES_VERSION
                and not _canonical_entities_is_valid(folder, transcript)):
            return True
        notes_text = (folder / "中文完整笔记.md").read_text(encoding="utf-8")
        briefing_text = (folder / "讲书稿.md").read_text(encoding="utf-8")
        summary_map = normalize_summary_claim_ids(
            load_json(folder / "summary_map.json"))
        if not _transcript_basis_is_current(folder, summary_map):
            return True
        errors, _warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map, briefing_text, content_map, notes_text)
        tts_errors = validate_tts_readiness(
            briefing_text, load_tts_lexicon(folder))
        ledger_path = folder / PREWRITE_FACT_CHECKS_FILENAME
        ledger_required = int(
            content_map.get("prewrite_fact_checks_version", 0) or 0
        ) >= PREWRITE_FACT_CHECKS_VERSION
        ledger_stale = (
            (ledger_required or ledger_path.exists())
            and not ledger_is_current(folder)
        )
        return bool(errors or summary_errors or tts_errors or ledger_stale)
    except (OSError, ValueError, TypeError):
        return True


def _correction_prompt(source_kind):
    prompt_path = Path(__file__).resolve().parent / "纠错提示词.md"
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    return (
        prompt
        + "\n\n## 本次受限任务\n"
        + f"当前 source_kind={source_kind!r}。\n"
        + "只允许创建或更新 转录_纠错.txt；不要修改 原始转录.txt、"
        + "transcript.raw.json 或 来源.md。来源状态由主流程统一写回。\n"
        + "本地 ASR 必须生成纠错稿；第三方文本只有在确实发现问题时生成。"
    )


def _structured_correction_ready(folder, raw):
    manifest_path = folder / CORRECTION_MANIFEST_NAME
    corrected_path = folder / "转录_纠错.txt"
    if not manifest_path.exists() or not corrected_path.exists():
        return False
    try:
        manifest = load_json(manifest_path)
        return not validate_correction_manifest(
            raw, manifest,
            rendered_text=corrected_path.read_text(encoding="utf-8"),
        )
    except (OSError, ValueError, TypeError):
        return False


def _run_structured_correction(folder, raw):
    """Correct consecutive batches and let deterministic code render text."""
    source_segments = [
        segment for segment in raw.get("segments", [])
        if (segment.get("text") or "").strip()
    ]
    corrected_items = []
    max_chars = _env_positive_int("CORRECTION_BATCH_CHARS", 30000)
    for batch_index, batch in enumerate(
            correction_batches(source_segments, max_chars=max_chars), start=1):
        expected_ids = [str(segment.get("id")) for segment in batch]
        compact_batch = [
            {
                "segment_id": segment.get("id"),
                "speaker": segment.get("speaker"),
                "text": segment.get("text", ""),
                "quality_flags": segment.get("quality_flags", []),
                "needs_redecode": bool(segment.get("needs_redecode")),
                "needs_review": bool(segment.get("needs_review")),
                "speaker_alignment": segment.get("speaker_alignment"),
                "refinement": segment.get("refinement"),
            }
            for segment in batch
        ]
        prompt_path = Path(__file__).resolve().parent / "纠错提示词.md"
        task = prompt_path.read_text(encoding="utf-8").strip() + f"""

## 本次结构化受限任务
当前 source_kind='local_asr'。本次只处理第 {batch_index} 批。输入如下：
{json.dumps(compact_batch, ensure_ascii=False)}

必须按输入顺序返回每个 segment_id，不能遗漏、重复或新增 ID。
corrected_text 只包含该 segment 的英文正文，不要写 speaker 标签。
广告、寒暄、口头语和真实重复也是原音频内容，不得因编辑价值低而删除。
你没有直接听音频，verification 不得填写 human_audio。普通低风险文字修正可使用 context_only；数字、金额、年份或专名变更必须由已有 refinement 支持并使用 alternate_decode，或经网页核对使用 external_entity_source；否则保留原文并标为 unresolved。
"""
        result = run_json_task(
            folder,
            task,
            batch_output_schema(),
            task_name=f"transcript_correction_{batch_index}",
            enable_search=True,
            model=os.environ.get("SUBAGENT_CORRECTION_MODEL", "") or None,
        )
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"纠错批次 {batch_index} 输出必须是对象")
        items = payload.get("segments", [])
        actual_ids = [
            str(item.get("segment_id"))
            for item in items if isinstance(item, dict)
        ]
        if any(
                isinstance(item, dict)
                and item.get("verification") == "human_audio"
                for item in items):
            raise RuntimeError(
                f"纠错批次 {batch_index} 无权声明 human_audio 验证")
        item_errors = validate_correction_batch(raw, items, expected_ids)
        if item_errors:
            raise RuntimeError(
                f"纠错批次 {batch_index} 输出无效: "
                + "; ".join(item_errors[:10]))
        if actual_ids != expected_ids:
            raise RuntimeError(
                f"纠错批次 {batch_index} segment 覆盖或顺序不匹配: "
                f"expected={expected_ids}, actual={actual_ids}")
        corrected_items.extend(items)
    manifest = build_manifest(raw, corrected_items)
    return write_correction_artifacts(folder, raw, manifest)


def _completeness_blocks_content(source_kind, raw):
    if source_kind != "local_asr" or not completeness_contract_required(raw):
        return False
    if completeness_enforcement_mode(raw) != "enforce":
        return False
    completeness = (raw.get("meta", {}) or {}).get("completeness")
    return (
        bool(validate_completeness_result(raw, completeness))
        or not isinstance(completeness, dict)
        or completeness.get("passed") is not True
    )


CLAIM_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "unit_id": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "claim_modalities": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "string",
                            "enum": sorted(CLAIM_MODALITIES),
                        },
                    },
                },
            },
        },
    },
}


def _repair_low_confidence_claims(folder, raw_path, error):
    unit_ids = sorted(set(re.findall(r"\bU\d{4,}\b", str(error))))
    if not unit_ids:
        raise error
    content_map_path = folder / "content_map.json"
    content_map = load_json(content_map_path)
    transcript = load_json(raw_path)
    segments = {
        segment.get("id"): segment
        for segment in transcript.get("segments", [])
        if isinstance(segment, dict) and segment.get("id")
    }
    units_by_id = {
        unit.get("id"): unit
        for unit in content_map.get("units", [])
        if isinstance(unit, dict) and unit.get("id")
    }
    missing = sorted(set(unit_ids) - set(units_by_id))
    if missing:
        raise RuntimeError(f"low confidence repair 引用了未知 unit: {missing}")
    repair_input = []
    for unit_id in unit_ids:
        unit = units_by_id[unit_id]
        segment_ids = unit.get("evidence", {}).get("segment_ids", [])
        repair_input.append({
            "unit_id": unit_id,
            "topic": unit.get("topic", ""),
            "claims": list(unit.get("claims", [])),
            "claim_modalities": list(unit.get("claim_modalities", [])),
            "low_evidence_notes": unit.get("claim_evidence_notes", {}),
            "segments": [
                {
                    "id": segment_id,
                    "text": segments.get(segment_id, {}).get("text", ""),
                }
                for segment_id in segment_ids
            ],
        })
    result = run_json_task(
        folder,
        f"""下面这些 unit 的 claim evidence 经过批量和单 unit 复核后仍为 low。
请依据 low_evidence_notes 和原始 segments，返回每个 unit 的完整替换 claims 和
逐条对应的 claim_modalities。只能删除转录不支持的从句、拆分或合并原 claim；不得增加新事实、改变 topic，
也不得修改 evidence。每条 claim 必须原子且由给定 segment 直接支持；每个 modality
必须是 actual_event、conditional、prediction、opinion、recommendation 或 general_claim，
并与 claims 数量和顺序完全一致。
必须恰好返回这些 unit，不能遗漏或新增。只返回 schema JSON，不修改文件。

输入：
{json.dumps(repair_input, ensure_ascii=False)}""",
        CLAIM_REPAIR_SCHEMA,
        task_name="repair_low_confidence_claims",
        model=os.environ.get("SUBAGENT_CLAIM_MODEL", "") or None,
        timeout=600,
    )
    payload = result.get("payload", {})
    returned = payload.get("units", []) if isinstance(payload, dict) else []
    returned_ids = [
        item.get("unit_id") for item in returned if isinstance(item, dict)
    ]
    if returned_ids != unit_ids:
        raise RuntimeError(
            "low confidence claim repair 返回集合不匹配: "
            f"expected={unit_ids}, actual={returned_ids}")
    for item in returned:
        claims = item.get("claims")
        if not isinstance(claims, list) or not claims or any(
                not isinstance(claim, str) or not claim.strip()
                for claim in claims):
            raise RuntimeError(
                f"{item.get('unit_id')}: repair claims 必须是非空字符串数组")
        modalities = item.get("claim_modalities")
        if (
                not isinstance(modalities, list)
                or len(modalities) != len(claims)
                or any(modality not in CLAIM_MODALITIES for modality in modalities)):
            raise RuntimeError(
                f"{item.get('unit_id')}: claim_modalities 必须与 claims "
                "数量一致且使用允许枚举")
        unit = units_by_id[item["unit_id"]]
        unit["claims"] = [claim.strip() for claim in claims]
        unit["claim_modalities"] = list(modalities)
        unit["claim_evidence"] = {}
        unit["claim_evidence_sha256"] = {}
        unit["claim_evidence_notes"] = {}
    save_json(content_map_path, content_map)
    return unit_ids


def _tts_lexicon_change_errors(briefing, existing, updated):
    errors = []
    for key, value in updated.items():
        if existing.get(key) == value:
            continue
        if not isinstance(key, str) or not isinstance(value, str):
            errors.append("TTS 词典新增项必须是字符串")
            continue
        key = key.strip()
        value = value.strip()
        if not key or key not in briefing:
            errors.append(f"TTS 词典 key 未精确出现在讲稿中: {key!r}")
        if len(key) > 48 or len(key.split()) > 6 \
                or len(re.findall(r"[一-鿿]", key)) > 6:
            errors.append(f"TTS 词典 key 范围过大: {key!r}")
        if not re.search(r"[A-Za-z0-9+/&()]", key):
            errors.append(f"TTS 词典 key 不是待修复读音 token: {key!r}")
        if not value or len(value) > 48:
            errors.append(f"TTS 词典读音长度无效: {key!r}")
        if re.search(r"[。！？!?；;]", value):
            errors.append(f"TTS 词典读音不得是句子: {key!r}")
        if re.search(r"\d|[+/]", value):
            errors.append(f"TTS 词典读音仍含难读数字或符号: {key!r}")
    return errors


def _ensure_tts_lexicon_ready(folder, briefing_path):
    briefing = briefing_path.read_text(encoding="utf-8")
    issues = validate_tts_readiness(briefing, load_tts_lexicon(folder))
    if not issues:
        return {"repaired": False, "entries": len(load_tts_lexicon(folder))}
    lexicon_path = folder / "tts_lexicon.json"
    existing = load_tts_lexicon(folder)
    run_edit_task(
        folder,
        f"""只修复讲书稿的 TTS 读音词典。当前确定性检查问题：
{json.dumps(issues, ensure_ascii=False)}

读取讲书稿.md，在 tts_lexicon.json 中为确实出现的完整词或完整短语增加精确映射。
输出必须是 JSON 对象 {{\"原词或短语\": \"自然中文朗读文本\"}}。
优先映射完整表达，例如 A/B，而不是单独映射 /；不得使用空 key、不得级联替换，
不得修改讲书稿或任何内容事实。混合大小写品牌、技术符号应按上下文给出自然读音；
拿不准时不要猜。只修改 tts_lexicon.json。""",
        task_name="tts_lexicon_pre_review",
        allowed_files=[lexicon_path],
        input_files=[briefing_path],
        required_files=[lexicon_path],
    )
    try:
        lexicon = load_tts_lexicon(folder)
    except Exception:
        atomic_write_json(lexicon_path, existing)
        raise
    change_errors = _tts_lexicon_change_errors(
        briefing, existing, lexicon)
    if change_errors:
        atomic_write_json(lexicon_path, existing)
        raise RuntimeError("; ".join(change_errors))
    remaining = validate_tts_readiness(briefing, lexicon)
    if remaining:
        raise RuntimeError(
            "TTS 词典受限修复后仍未就绪: " + "; ".join(remaining))
    return {"repaired": True, "entries": len(lexicon)}


def run_content_pipeline(folder, title, run_report=None, force=False):
    """Run correction, content mapping, writing, evidence, and hash enrichment."""
    folder = Path(folder).resolve()
    raw_path = folder / "transcript.raw.json"
    transcript_path = folder / "原始转录.txt"
    if not raw_path.exists() or not transcript_path.exists():
        raise RuntimeError("subagent 内容流程缺少原始转录证据")

    raw = load_json(raw_path)
    source_kind = effective_source_kind(folder, raw)
    correction_path = folder / "转录_纠错.txt"
    source_path = folder / "来源.md"
    correction_inputs = [
        path for path in (raw_path, transcript_path, source_path)
        if path.exists()
    ]

    contract_required = (
        source_kind == "local_asr" and correction_contract_required(raw)
    )
    if _completeness_blocks_content(source_kind, raw):
        print(
            "[内容][阻断] 新 ASR revision 的语音完整性检查未通过",
            flush=True,
        )
        return False

    transcript_ready = (
        not force
        and _accepted_transcript_status(
            quality_metadata(folder).get("transcript_status"))
        and (
            source_kind not in ASR_SOURCE_KINDS
            or (
                _structured_correction_ready(folder, raw)
                if contract_required else correction_path.exists()
            )
        )
    )
    with _stage(run_report, "subagent_transcript_correction") as stage:
        if transcript_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "accepted transcript status",
                    "corrected": correction_path.exists(),
                    "structured": contract_required,
                })
        elif contract_required:
            result = _run_structured_correction(folder, raw)
            update_transcript_status(
                folder, "已纠错（结构化）", "corrected_structured")
            if stage is not None:
                stage.metrics.update(result["summary"])
                stage.metrics["structured"] = True
        else:
            run_edit_task(
                folder,
                _correction_prompt(source_kind),
                task_name="transcript_correction",
                allowed_files=[correction_path],
                input_files=correction_inputs,
                required_files=(
                    [correction_path]
                    if source_kind in ASR_SOURCE_KINDS else []),
                remove_missing_outputs=force,
            )
            if (
                    source_kind in ASR_SOURCE_KINDS
                    and not correction_path.exists()):
                if stage is not None:
                    stage.fail("ASR subagent 未生成 转录_纠错.txt")
                return False
            corrected = correction_path.exists()
            update_transcript_status(
                folder,
                "已纠错（subagent）"
                if corrected else "可接受（subagent 已抽查）",
                "corrected" if corrected else "sample_checked",
            )
            if stage is not None:
                stage.metrics["corrected"] = corrected

    content_map_path = folder / "content_map.json"
    content_map_ready = not force and _content_map_is_valid(folder)
    with _stage(run_report, "subagent_content_map") as stage:
        if content_map_ready:
            payload = load_json(content_map_path)
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "valid content map",
                    "unit_count": len(payload.get("units", [])),
                })
        else:
            _ensure_content_map(folder, title, force=force)
            map_inputs = [
                path for path in (
                    raw_path, transcript_path, correction_path, source_path)
                if path.exists()
            ]
            run_edit_task(
                folder,
                f"""读取 transcript.raw.json、原始转录.txt，
以及存在时的 转录_纠错.txt。
整理 content_map.json：
- 保留 schema_version、evidence_mode 和 source_accountability_version=1；
- 将相邻转录片段合并为有完整语义的 unit；
- 补充 topic、claims、reasoning、examples、numbers、terms；
- 标记 importance 和 status；完成后的 status 只能是 included、condensed、excluded，
  禁止使用 expanded、selected、skipped 等自定义值；
- 保留并正确填写 evidence.segment_ids；
- timestamp evidence 必须使用真实时间，text_anchor evidence 不得伪造时间；
- high/medium unit 必须有 claims；每条 claim 必须在 claim_modalities 中按顺序标记 actual_event、conditional、prediction、opinion、recommendation 或 general_claim；
- 条件句不得升级成已发生事件，预测和观点不得升级成客观事实；
- transcript.raw.json 的每个非空 segment 必须至少进入一个 unit；
- 广告、纯寒暄、节目操作信息等真实语音必须保留证据，可标记 status=excluded；
- 每个 unit 的 evidence.segment_ids 都必须至少包含一个真实源 segment；不得创建无源 segment 的 unit；
- excluded 单元的 claims 必须是空数组，并填写 exclusion_type 和具体 notes；
- excluded 必须填写 exclusion_type（advertisement/housekeeping/banter/non_speech/duplicate/technical_noise/other）和具体 notes；
- 有上下文价值但无需展开的内容使用 condensed，不要静默遗漏；
- 无法判断的内容使用 unresolved 并停止，不得猜测排除；
- 不要写 claim_evidence，后续由独立 subagent 生成；
- content_map 只能记录转录实际说法：即使知道外部资料不同，也只能在 claims 中保留明确说话人归因，
  不得把网页纠正、编辑判断或核查结论合并进由 Sxxxx 片段锚定的 claim；
- 外部纠正由后续 editorial_fact_checks.json 单独承载；
- 只修改 content_map.json，不修改其他文件。""",
                task_name="content_map",
                allowed_files=[content_map_path],
                input_files=map_inputs,
                required_files=[content_map_path],
            )
            payload = load_json(content_map_path)
            normalize_detail_items(payload)
            payload["prewrite_fact_checks_version"] = (
                PREWRITE_FACT_CHECKS_VERSION)
            save_json(content_map_path, payload)
            if payload.get("source_accountability_version") != 1:
                if stage is not None:
                    stage.fail("content_map.json 缺少 source accountability contract")
                return False
            if not isinstance(payload.get("units"), list) or not payload["units"]:
                if stage is not None:
                    stage.fail("content_map.json 没有有效 units")
                return False
            _validate_content_map_stage_statuses(payload)
            if stage is not None:
                stage.metrics["unit_count"] = len(payload["units"])

    with _stage(run_report, "subagent_claim_evidence") as stage:
        if content_map_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "valid content map",
                })
        else:
            claim_kwargs = {
                "model": os.environ.get("SUBAGENT_CLAIM_MODEL", ""),
                "effort": os.environ.get("SUBAGENT_CLAIM_EFFORT", "high"),
                "max_batch_chars": _env_positive_int(
                    "CLAIM_EVIDENCE_BATCH_CHARS", 35000),
                "concurrency": _env_positive_int(
                    "CLAIM_EVIDENCE_CONCURRENCY", 3),
            }
            try:
                metrics = refine_claim_evidence(folder, **claim_kwargs)
            except RuntimeError as exc:
                if "claim evidence confidence=low" not in str(exc):
                    raise
                repair_unit_ids = _repair_low_confidence_claims(
                    folder, raw_path, exc)
                metrics = refine_claim_evidence(
                    folder,
                    unit_ids=repair_unit_ids,
                    **claim_kwargs,
                )
                metrics["claim_repair_unit_count"] = len(repair_unit_ids)
            if stage is not None:
                stage.metrics.update(metrics)

    entities_path = folder / "canonical_entities.json"
    entities_ready = (
        not force and content_map_ready
        and _canonical_entities_is_valid(folder, raw)
    )
    with _stage(run_report, "subagent_canonical_entities") as stage:
        if entities_ready:
            entities = load_json(entities_path)
            content_map = load_json(content_map_path)
            if content_map.get("canonical_entities_contract_version") != (
                    CANONICAL_ENTITIES_VERSION):
                content_map["canonical_entities_contract_version"] = (
                    CANONICAL_ENTITIES_VERSION)
                save_json(content_map_path, content_map)
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "valid canonical entity ledger",
                    "entity_count": len(entities.get("entities", [])),
                })
        else:
            result = run_json_task(
                folder,
                """读取 transcript.raw.json、转录_纠错.txt（若存在）和 content_map.json。
为所有可能进入中文笔记或讲稿的人名、公司、产品、机构、作品标题、地点和关键术语
建立规范实体表。observed_names 必须列出转录或 content-map 中实际出现的拼写，包括
ASR 错词和大小写变体；canonical_name 使用官方规范名称；public_aliases 只列允许在
公开中文稿继续使用的安全简称，不得包含 ASR 错词。person/company/product/
institution/title 必须提供官方或一手来源 URL。segment_ids 只能引用实体实际出现的
Sxxxx。不要把普通名词、整句 claim 或未经来源确认的猜测当作实体。只返回 schema
JSON，不修改文件。""",
                CANONICAL_ENTITIES_SCHEMA,
                task_name="canonical_entities",
                enable_search=True,
                model=os.environ.get("SUBAGENT_ENTITY_MODEL", "") or None,
                timeout=1200,
            )
            generated = result.get("payload")
            if not isinstance(generated, dict):
                raise RuntimeError("canonical entity subagent 输出必须是对象")
            entities = {
                "schema_version": CANONICAL_ENTITIES_VERSION,
                "evidence_revision": (
                    (raw.get("evidence", {}) or {}).get("revision_sha256")
                    or (raw.get("evidence", {}) or {}).get("transcript_sha256")
                ),
                "entities": generated.get("entities"),
            }
            entity_errors = validate_canonical_entities(entities, raw)
            if entity_errors:
                if stage is not None:
                    stage.fail("; ".join(entity_errors[:10]))
                return False
            save_json(entities_path, entities)
            content_map = load_json(content_map_path)
            content_map["canonical_entities_contract_version"] = (
                CANONICAL_ENTITIES_VERSION)
            save_json(content_map_path, content_map)
            if stage is not None:
                stage.metrics.update({
                    "entity_count": len(entities.get("entities", [])),
                    "structured_output": True,
                })

    with _stage(run_report, "subagent_prewrite_fact_checks") as stage:
        ledger_path = folder / PREWRITE_FACT_CHECKS_FILENAME
        if not force and ledger_is_current(folder):
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "current pre-writing fact-check ledger",
                })
        else:
            metrics = run_prewrite_fact_checks(
                folder,
                model=os.environ.get("SUBAGENT_FACT_CHECK_MODEL", ""),
                effort=os.environ.get(
                    "SUBAGENT_FACT_CHECK_EFFORT", "high"),
            )
            if stage is not None:
                stage.metrics.update(metrics)

    with _stage(run_report, "subagent_content_writing") as stage:
        notes_path = folder / "中文完整笔记.md"
        briefing_path = folder / "讲书稿.md"
        summary_path = folder / "summary_map.json"
        run_edit_task(
            folder,
            f"""读取 transcript.raw.json、原始转录.txt，
如果存在则读取 转录_纠错.txt，并读取已经完成的 content_map.json、
canonical_entities.json 和 editorial_fact_checks.json。content_map 只表示节目实际说了什么；
editorial_fact_checks.json 是绑定当前 content_map 和转录基准的外部核查台账，
只能把其中有 URL 支持的 editorial_correction 作为自然事实限定写入笔记和讲稿，
不得把外部纠正反写进 content_map 或伪装成由 Sxxxx 片段直接支持。

按顺序完成：
1. 先写 中文完整笔记.md，逐项覆盖所有 included high/medium unit，以及所有
condensed unit 的实质 claims、numbers 和 examples，保留人物归属、限定条件、时间顺序和推理链。
2. 再写 讲书稿.md，将完整笔记整理为适合中文收听的讲书稿；included high/medium
必须覆盖，condensed 可按收听价值选择是否进入。
3. 最后写 summary_map.json，映射章节标题、unit_ids 和 claim_ids；并显式填写
notes_claim_ids、notes_number_ids、notes_example_ids，只能列入中文完整笔记正文实际
覆盖的 claim/number/example。若仍有 notes 必需项未覆盖，先补写笔记，再加入对应 ID。

要求：
- 必须使用 canonical_entities.json 中的 canonical_name；observed_names 只用于定位原始错误，不得泄漏到公开文本；
- 不得编造转录之外的观点；
- 不得遗漏 included high/medium 的 claims、numbers 或 examples；所有 condensed
  实质 claim 必须进入完整笔记，但进入讲稿可选；
- 必须保持 content_map.claim_modalities：conditional 继续使用“如果/即使/可能”，prediction 继续标明预测，opinion/recommendation 保留说话人归因；
- 中文完整笔记必须逐项覆盖 content_map 的 number_items 和 example_items；summary_map 分别用完整 ID（如 U0001-N01、U0001-E01）声明；
- 完成前按中文汉字数自检：中文完整笔记必须至少比讲书稿多百分之十五；不足时只能
  从转录和 content_map 补充证据细节、数字范围、例子、限定条件与推理链，禁止用重复或空话凑字数；
- excluded unit 不得进入中文完整笔记、讲书稿或 summary_map；
- editorial_fact_checks.json 的 issue_inventory 中所有 critical/high/medium 问题
  必须在写作时一次性处理；不得修完第一个问题就停止，也不得遗漏同一 claim 的其他问题；
- 外部纠正必须保留节目原话的说话人归因，并自然说明官方、一手论文或原始报告口径；
- 不要修改 content_map.json、editorial_fact_checks.json 或任何证据文件；
- 第一个 ## 前写 50–100 字全局导览；
- 每章正文必须包含 420–900 个中文汉字（按汉字计数，不是总字符），禁止超过 1000；
- 章节标题必须准确概括该章 unit，不得用一个话题标题承载无关的后续主题；
- 讲稿必须保留影响理解的事实状态，例如“节目称”“报道称”“仍在洽谈”“这是预测而非已发生结果”；
- 中文完整笔记和讲稿都禁止出现面向内部的审查决策语言，例如“这里不采用”“这里不保留”“本稿未独立核实”“由于口径不同因此删除”；审查理由只写入 JSON 审查产物，不写给听众；
- 为避免确定性审计语言误判，即使讨论政策监督也不要使用“审查过程”这一短语，改用“决策程序”“审批流程”或更具体的领域表达；
- 不得向听众描述转录或证据处理状态，例如“转录口述”“转录不清”“识别不清”“转录没有确认”；应直接写自然的来源限定，如“节目称”“公开报告估算”“节目没有说明”；
- 对嘉宾针对第三方研究方法、实验次数或外部机构判断的指控，只能采用 content_map 明确保留且有来源支持的精度；否则保留说话人归因并自然概括，不得从转录重新加入被 content_map 删除的精确次数或机构背书；
- 若删除无法核实的精确数字，直接用自然、带归因的概括表达核心观点，不向听众解释后台为什么删数；
- 讲稿每个 ## 标题必须与 summary_map.chapters[].title 逐字一致；
- summary_map 先写结构，正文哈希由主脚本补齐。""",
            task_name="content_writing",
            allowed_files=[notes_path, briefing_path, summary_path],
            input_files=[
                path for path in (
                    raw_path, transcript_path, correction_path,
                    content_map_path, entities_path, source_path, ledger_path)
                if path.exists()
            ],
            required_files=[notes_path, briefing_path, summary_path],
        )
        missing = [
            path.name for path in (notes_path, briefing_path, summary_path)
            if not path.exists()
        ]
        if missing:
            if stage is not None:
                stage.fail(f"内容 subagent 缺少输出: {missing}")
            return False
        if stage is not None:
            stage.metrics["outputs"] = [
                notes_path.name, briefing_path.name, summary_path.name]

    with _stage(run_report, "deterministic_evidence_enrichment") as stage:
        transcript = load_json(raw_path)
        content_map = load_json(content_map_path)
        summary_path = folder / "summary_map.json"
        notes_text = (folder / "中文完整笔记.md").read_text(encoding="utf-8")
        finalized = finalize_content_package(folder)
        briefing_text = finalized["briefing"]
        summary_map = finalized["summary_map"]
        normalization_changes = finalized["normalization_changes"]
        tts_readiness = _ensure_tts_lexicon_ready(
            folder, folder / "讲书稿.md")
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        summary_map = enrich_summary_map_evidence(
            summary_map,
            notes_text,
            content_map,
            briefing_text,
        )
        summary_map["transcript_basis"] = _transcript_basis(folder)
        save_json(content_map_path, content_map)
        save_json(summary_path, summary_map)
        errors, warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map,
            briefing_text,
            content_map,
            notes_text,
        )
        entity_alias_errors = public_entity_alias_errors(
            load_json(entities_path), notes_text, briefing_text)
        if errors or summary_errors or entity_alias_errors:
            all_errors = errors + summary_errors + entity_alias_errors
            if stage is not None:
                stage.fail("; ".join(all_errors[:10]))
            return False
        if stage is not None:
            stage.metrics.update({
                "unit_count": len(content_map.get("units", [])),
                "warning_count": len(warnings),
                "normalization_changes": normalization_changes,
                "tts_lexicon_entries": tts_readiness["entries"],
                "tts_lexicon_repaired": tts_readiness["repaired"],
            })

    sync_episode_state(folder)
    return True

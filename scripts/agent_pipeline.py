"""Subagent-orchestrated content production for a single episode."""
import hashlib
import json
import os
import re
from difflib import SequenceMatcher
from contextlib import nullcontext
from pathlib import Path

from scripts.atomic_io import atomic_write_json
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
    normalize_generated_unit_ids,
    normalize_detail_items,
    normalize_summary_claim_ids,
    save_json,
    CLAIM_MODALITIES,
    STATUS_VALUES,
    validate_content_map,
    validate_summary_map,
)
from scripts.content_finalizer import (
    ContentFinalizationError,
    finalize_content_package,
    validate_tts_readiness,
)
from scripts.episode import (
    quality_metadata,
    sync_episode_state,
    update_transcript_status,
)
from scripts.evidence import ASR_SOURCE_KINDS, effective_source_kind
from scripts.subagent import SubagentError, run_edit_task, run_json_task
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


def _third_party_correction_is_complete(folder):
    """Reject truncated optional corrections from third-party transcript tasks."""
    folder = Path(folder)
    raw_path = folder / "原始转录.txt"
    corrected_path = folder / "转录_纠错.txt"
    if not raw_path.exists() or not corrected_path.exists():
        return False
    raw = raw_path.read_text(encoding="utf-8")
    corrected = corrected_path.read_text(encoding="utf-8")
    if not corrected.strip():
        return False
    # Third-party correction is plain-text and has no segment manifest.  It may
    # polish wording, but it must not silently become a short prefix of the
    # evidence.  A conservative ratio catches truncated runner output while
    # allowing normal editorial compression of whitespace and punctuation.
    return len(corrected) >= max(1000, round(len(raw) * 0.6))


def _transcript_basis(folder):
    corrected = folder / "转录_纠错.txt"
    path = corrected if corrected.exists() else folder / "原始转录.txt"
    return {
        "file": path.name,
        "sha256": body_sha256(path.read_text(encoding="utf-8")),
    }


WRITING_INPUTS_VERSION = 1


def _semantic_content_map_hash(path):
    payload = load_json(path)
    projection = {
        "schema_version": payload.get("schema_version"),
        "detail_items_version": payload.get("detail_items_version"),
        "units": [
            {
                key: unit.get(key)
                for key in (
                    "id", "topic", "speaker", "claims", "claim_modalities",
                    "reasoning", "examples", "numbers", "terms", "importance",
                    "status", "exclusion_type", "notes",
                )
                if key in unit
            }
            for unit in payload.get("units", []) or []
            if isinstance(unit, dict)
        ],
    }
    return body_sha256(json.dumps(
        projection, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ))


def _semantic_entities_hash(path):
    payload = load_json(path)
    projection = [
        {
            "entity_id": item.get("entity_id"),
            "canonical_name": item.get("canonical_name"),
        }
        for item in payload.get("entities", []) or []
        if isinstance(item, dict)
    ]
    return body_sha256(json.dumps(
        projection, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ))


def _writing_input_hashes(folder):
    folder = Path(folder)
    result = {
        "transcript_basis": _transcript_basis(folder),
    }
    for name in (
            "content_map.json", "canonical_entities.json",
            PREWRITE_FACT_CHECKS_FILENAME):
        path = folder / name
        if name == "content_map.json" and path.exists():
            result[name] = _semantic_content_map_hash(path)
        elif name == "canonical_entities.json" and path.exists():
            result[name] = _semantic_entities_hash(path)
        else:
            result[name] = (
                body_sha256(path.read_text(encoding="utf-8"))
                if path.exists() else None
            )
    return result


def _writing_inputs_are_current(folder, summary_map):
    version = summary_map.get("writing_inputs_version")
    if version is None:
        return True
    return (
        version == WRITING_INPUTS_VERSION
        and summary_map.get("writing_inputs") == _writing_input_hashes(folder)
    )


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
    # A current rejected review is actionable input for content recovery: the
    # next public-entry run must regenerate prose before requesting another
    # independent review, rather than reusing the rejected package forever.
    review_path = folder / "ai_review.json"
    if review_path.exists():
        try:
            review = load_json(review_path)
        except (OSError, ValueError, TypeError):
            return True
        if review.get("passed") is False:
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
        if not _writing_inputs_are_current(folder, summary_map):
            return True
        errors, _warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map, briefing_text, content_map, notes_text)
        tts_errors = validate_tts_readiness(
            briefing_text, load_tts_lexicon(folder))
        alias_errors = public_entity_alias_errors(
            load_json(folder / "canonical_entities.json"),
            notes_text,
            briefing_text,
        )
        ledger_path = folder / PREWRITE_FACT_CHECKS_FILENAME
        ledger_required = int(
            content_map.get("prewrite_fact_checks_version", 0) or 0
        ) >= PREWRITE_FACT_CHECKS_VERSION
        ledger_stale = (
            (ledger_required or ledger_path.exists())
            and not ledger_is_current(folder)
        )
        return bool(
            errors or summary_errors or tts_errors or alias_errors or ledger_stale
        )
    except (OSError, ValueError, TypeError):
        return True


def _writing_artifacts_are_current(folder, *, require_bound_inputs=False):
    """Validate prose reuse; upstream repairs require explicit input bindings."""
    folder = Path(folder)
    required = [
        folder / "transcript.raw.json",
        folder / "content_map.json",
        folder / "中文完整笔记.md",
        folder / "讲书稿.md",
        folder / "summary_map.json",
    ]
    if any(not path.exists() for path in required):
        return False
    try:
        transcript = load_json(folder / "transcript.raw.json")
        content_map = load_json(folder / "content_map.json")
        notes_text = (folder / "中文完整笔记.md").read_text(
            encoding="utf-8")
        briefing_text = (folder / "讲书稿.md").read_text(
            encoding="utf-8")
        summary_map = normalize_summary_claim_ids(
            load_json(folder / "summary_map.json"))
        if (
                require_bound_inputs
                and summary_map.get("writing_inputs_version") != WRITING_INPUTS_VERSION):
            return False
        if not _transcript_basis_is_current(folder, summary_map):
            return False
        if not _writing_inputs_are_current(folder, summary_map):
            return False
        map_errors, _warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map, briefing_text, content_map, notes_text)
        ledger_path = folder / PREWRITE_FACT_CHECKS_FILENAME
        ledger_required = int(
            content_map.get("prewrite_fact_checks_version", 0) or 0
        ) >= PREWRITE_FACT_CHECKS_VERSION
        ledger_current = (
            not (ledger_required or ledger_path.exists())
            or ledger_is_current(folder)
        )
        return not map_errors and not summary_errors and ledger_current
    except (OSError, ValueError, TypeError):
        return False


def _writing_prompt():
    rules = (Path(__file__).resolve().parent / "讲稿提示词.md").read_text(
        encoding="utf-8").strip()
    return rules + """

## 本次受限任务

读取 transcript.raw.json、原始转录.txt、存在时的 转录_纠错.txt，以及
content_map.json、canonical_entities.json、editorial_fact_checks.json。
按规则依次生成 中文完整笔记.md、讲书稿.md、summary_map.json。
只允许修改这三个输出；不得修改输入台账或任何证据文件，不要运行内部阶段命令。
summary_map 先写结构，正文哈希与 writing_inputs 绑定由主流程最终化补齐。

## 交付前机械检查（优先执行）
- 统计讲书稿.md 中所有以“## ”开头的章节标题；summary_map.json 的 chapters 必须逐项一一对应，数量必须完全相同。
- summary_map.json 每个 chapter.title 必须与讲书稿对应标题逐字一致；不得少一章、多一章或把两章合并成一个映射。
- 每个章节的 unit_ids、claim_ids 必须来自该章节实际覆盖的 content_map；宁可补齐映射，也不要交付数量不一致的 summary_map。
- 完成前重新读取这三个输出并修复上述机械不一致；不要把检查说明写入公开稿。

## 终审数字与证据修复要求
如果目录中存在 ai_review.json 且 passed=false，必须逐条处理其中的 numbers、factuality、attribution 和 tts issues：
- 公开来源无法支持的精确金额、百分比、倍数、概率、季度数和市场份额，不得只补一句“节目称”后继续保留；应改为趋势性或范围性表述，并保留“节目称/嘉宾估计”归因。
- 有官方或一手来源支持的数字才保留精确值，并保持日期、财季、统计口径和来源限定。
- 第三方指控、动机判断和法律推测只能写成“节目转述/嘉宾推测/节目观点”，不得写成旁白事实。
- 公开稿中的每个英文专名、缩写和混合短语都必须能由 tts_lexicon.json 安全朗读；不得把后台审查说明写进公开稿。
- 严禁在公开稿出现“独立核验”“不能写成”“这里只保留”“不把……当作事实”“以下是节目口径”“审查”“证据链”“fact check”“发布标准”等编辑流程语言。改写成面向听众的自然表达，例如“节目当时的说法是……”“嘉宾据此推测……”“公开资料尚不足以确认……”。
"""


def _repair_summary_map_contract(
        folder, briefing_path, summary_path, content_map_path, notes_path,
        error):
    """Repair only summary bindings after deterministic chapter validation."""
    canonical_titles = re.findall(
        r"(?m)^##\s+(.+?)\s*$",
        briefing_path.read_text(encoding="utf-8"),
    )
    repair_prompt = f"""只修复 summary_map.json 的结构绑定，不改写讲书稿、笔记、content_map 或任何事实。

确定性最终化报错：{error}

读取讲书稿.md，逐行统计所有以“## ”开头的章节；再读取当前 summary_map.json、content_map.json 和中文完整笔记.md。
确定性章节标题顺序为：{json.dumps(canonical_titles, ensure_ascii=False)}。
必须让 summary_map.json 的 chapters 与讲书稿章节一一对应、数量完全相同、title 逐字一致。
如果讲书稿存在 summary_map 缺失的章节，为该章节补入基于实际正文和 content_map 的 unit_ids、claim_ids；不得捏造事实、不得删除讲书稿章节、不得把两章合并成一章。
保留所有已有正确映射，只做完成机械绑定所必需的最小修改。
完成后重新读取并核对章节数量和标题。只修改 summary_map.json。"""
    original_summary = summary_path.read_text(encoding="utf-8")
    repair_attempts = [
        ("summary_map_contract_repair", repair_prompt),
        ("summary_map_contract_repair_retry_1", repair_prompt +
         "\n这是结构修复重试。重新读取三个输入文件，逐项核对章节数量；"
         "如果上一轮已经修复，保持正确结果，不要新增或删除正文。"),
        ("summary_map_contract_repair_retry_2", repair_prompt +
         "\n这是最后一次结构修复重试。必须只写入一个完整、可解析的 JSON 对象；"
         "如果上一轮已经修复，保持正确结果，不要新增或删除正文。"),
    ]
    for task_name, prompt in repair_attempts:
        run_edit_task(
            folder,
            prompt,
            task_name=task_name,
            allowed_files=[summary_path],
            input_files=[briefing_path, content_map_path, notes_path],
            required_files=[summary_path],
        )
        try:
            load_json(summary_path)
            return
        except (OSError, ValueError, TypeError):
            # Never pass a malformed repair artifact to the finalizer; give
            # the next attempt the last known valid document instead.
            summary_path.write_text(original_summary, encoding="utf-8")
    summary_path.write_text(original_summary, encoding="utf-8")


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


def _load_correction_batch_cache(path, fingerprint, raw, expected_ids):
    """Reuse only fully validated results for the exact correction inputs."""
    try:
        cached = load_json(path)
        if not isinstance(cached, dict) or cached.get("fingerprint") != fingerprint:
            return None
        items = cached.get("segments")
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            return None
        if [str(x.get("segment_id")) for x in items] != expected_ids:
            return None
        if any(x.get("verification") == "human_audio" for x in items):
            return None
        if validate_correction_batch(raw, items, expected_ids):
            return None
        return items
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _run_structured_correction(folder, raw):
    """Correct consecutive batches, resuming validated batches after failure."""
    folder = Path(folder)
    # Bind the entire evidence revision, not just segment text: timestamps,
    # speaker assignments and refinement provenance can affect validation.
    raw_hash = hashlib.sha256(json.dumps(
        raw, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    schema = batch_output_schema()
    model = os.environ.get("SUBAGENT_CORRECTION_MODEL", "") or None
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

严格合同提醒：
- status=corrected 时 corrected_text 必须确实不同，unresolved 必须是空数组；如果无法提出不同且有依据的文本，就使用 status=unresolved 并逐字复制输入；不要把“已修正但仍不确定”混用为 corrected+unresolved。
- status=unresolved 时 corrected_text 必须逐字保留输入原文，verification 必须为 unresolved，unresolved 只能填写简短的复核原因（例如“专名无法从上下文确认”），不得把人名、数字、公司名或整段待确认文本本身放入 unresolved 数组。
- 能从节目上下文或公开实体来源确认的专名（例如人物、公司、产品）应直接使用 corrected+external_entity_source；不要因为它是专名就标 unresolved。
- 机械硬门：只要原文或 corrected_text 含人名、公司名、产品名、数字、金额、年份、百分号或大写专名，status=corrected 的 verification 只能是 alternate_decode 或 external_entity_source；绝对不能使用 context_only。
"""
        fingerprint = hashlib.sha256(json.dumps({
            "cache_version": 1,
            "raw_sha256": raw_hash,
            "runner_settings": {
                key: os.environ.get(key, "") for key in (
                    "SUBAGENT_MODEL", "SUBAGENT_PI_PROVIDER", "SUBAGENT_PI_MODEL",
                    "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL",
                )
            },
            "task": task,
            "schema": schema,
            "model": model,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        cache_path = folder / "correction_batches" / f"{batch_index:03d}.json"
        cached_items = _load_correction_batch_cache(
            cache_path, fingerprint, raw, expected_ids)
        if cached_items is not None:
            corrected_items.extend(cached_items)
            continue
        result = run_json_task(
            folder,
            task,
            schema,
            task_name=f"transcript_correction_{batch_index}",
            enable_search=True,
            model=model,
        )
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"纠错批次 {batch_index} 输出必须是对象")
        items = payload.get("segments", [])
        # Models occasionally mark an unchanged segment as corrected.  Normalize
        # that bookkeeping error without changing any transcript text; flagged
        # segments remain explicitly unresolved so the correction contract does
        # not silently bless an unverified rewrite.
        source_by_id = {
            str(segment.get("id")): segment for segment in batch
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            source = source_by_id.get(str(item.get("segment_id")))
            if source is None or item.get("status") != "corrected":
                continue
            if str(item.get("corrected_text", "")).strip() != str(
                    source.get("text", "")).strip():
                continue
            flagged = bool(
                source.get("needs_redecode")
                or source.get("needs_review")
                or source.get("speaker_alignment") == "unresolved"
            )
            if flagged:
                item.update({
                    "status": "unresolved",
                    "corrected_text": str(source.get("text", "")).strip(),
                    "change_types": [],
                    "verification": "unresolved",
                    "unresolved": ["未发现可验证的文本改动"],
                })
            else:
                item.update({
                    "status": "unchanged",
                    "corrected_text": str(source.get("text", "")).strip(),
                    "change_types": [],
                    "verification": "not_required",
                    "unresolved": [],
                })
            if item.get("status") == "corrected":
                source_text = str(source.get("text", "")).strip()
                corrected_text = str(item.get("corrected_text", "")).strip()
                source_words = max(1, len(source_text.split()))
                corrected_words = max(1, len(corrected_text.split()))
                similarity = SequenceMatcher(
                    None, source_text.lower(), corrected_text.lower(),
                    autojunk=False).ratio()
                if (
                        corrected_text
                        and (corrected_words / source_words < 0.6
                             or corrected_words / source_words > 1.6
                             or similarity < 0.55)
                        and item.get("verification") != "human_audio"):
                    # Never accept a model rewrite that dropped or merged
                    # source material.  Preserve the evidence verbatim and
                    # make the uncertainty explicit instead.
                    item.update({
                        "status": "unresolved",
                        "corrected_text": source_text,
                        "change_types": [],
                        "verification": "unresolved",
                        "unresolved": ["模型改写幅度超过可验证范围"],
                    })
        for item in items:
            if not isinstance(item, dict) or item.get("status") != "corrected":
                continue
            source = source_by_id.get(str(item.get("segment_id")))
            if source is None:
                continue
            source_text = str(source.get("text", "")).strip()
            corrected_text = str(item.get("corrected_text", "")).strip()
            source_words = max(1, len(source_text.split()))
            corrected_words = max(1, len(corrected_text.split()))
            similarity = SequenceMatcher(
                None, source_text.lower(), corrected_text.lower(),
                autojunk=False).ratio()
            invalid_magnitude = (
                corrected_text
                and (corrected_words / source_words < 0.6
                     or corrected_words / source_words > 1.6
                     or similarity < 0.55)
                and item.get("verification") != "human_audio"
            )
            high_risk = bool(re.search(
                r"(?:[$€£¥]|\b\d+(?:[,.]\d+)*(?:%|x|k|m|b)?\b|"
                r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*)+\b)",
                source_text + " " + corrected_text,
            ))
            invalid_verification = (
                high_risk
                and item.get("verification") not in {
                    "alternate_decode", "external_entity_source", "human_audio"
                }
            )
            if invalid_magnitude or invalid_verification:
                item.update({
                    "status": "unresolved",
                    "corrected_text": source_text,
                    "change_types": [],
                    "verification": "unresolved",
                    "unresolved": [
                        "模型改写幅度超过可验证范围"
                        if invalid_magnitude else "高风险纠错缺少独立验证"
                    ],
                })
        for item in items:
            if not isinstance(item, dict):
                continue
            source = source_by_id.get(str(item.get("segment_id")))
            if source is None:
                continue
            source_text = str(source.get("text", "")).strip()
            if item.get("status") in {"unchanged", "unresolved"} and str(
                    item.get("corrected_text", "")).strip() != source_text:
                item.update({
                    "status": "unresolved",
                    "corrected_text": source_text,
                    "change_types": [],
                    "verification": "unresolved",
                    "unresolved": ["模型未提供可验证的文本修正"],
                })
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
        # A later batch may fail. Persist this batch only after all existing
        # evidence/coverage checks; never publish a partial corrected transcript.
        atomic_write_json(cache_path, {
            "fingerprint": fingerprint,
            "segments": items,
        })
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
    repair_prompt = f"""只修复讲书稿的 TTS 读音词典。当前确定性检查问题：
{json.dumps(issues, ensure_ascii=False)}

读取讲书稿.md，在 tts_lexicon.json 中为确实出现的完整词或完整短语增加精确映射。
输出必须是 JSON 对象 {{\"原词或短语\": \"自然中文朗读文本\"}}。
每个新增 key 必须是讲稿中实际出现的单个英文/数字/符号 token 或短专名，
不得包含中文释义、中文长短语、连字符两侧的中文组合、整句或机构描述；
key 最多 48 个字符、最多 6 个空格分词、最多 6 个汉字，并且必须含有英文、数字、
加号、斜杠、& 或括号之一。优先映射完整表达，例如 A/B，而不是单独映射 /；
不得使用空 key、不得级联替换，不得修改讲书稿或任何内容事实。
混合大小写品牌、技术符号应按上下文给出自然读音；拿不准时不要猜。
只修改 tts_lexicon.json。"""
    lexicon = None
    parse_error = None
    for attempt in range(2):
        if attempt:
            atomic_write_json(lexicon_path, existing)
        run_edit_task(
            folder,
            repair_prompt + (
                "\n上一次输出不是合法 JSON。重新读取并只写入一个完整、可解析的 JSON 对象，"
                "不要写 Markdown 代码围栏。"
                if attempt else ""
            ),
            task_name=("tts_lexicon_pre_review"
                       if not attempt else "tts_lexicon_pre_review_retry"),
            allowed_files=[lexicon_path],
            input_files=[briefing_path],
            required_files=[lexicon_path],
        )
        try:
            lexicon = load_tts_lexicon(folder)
            break
        except Exception as exc:
            parse_error = exc
    if lexicon is None:
        atomic_write_json(lexicon_path, existing)
        raise parse_error
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
    if (
            source_kind not in ASR_SOURCE_KINDS
            and correction_path.exists()
            and not _third_party_correction_is_complete(folder)):
        # This file is optional for web/official transcripts.  Keep the raw
        # evidence authoritative rather than allowing a truncated runner
        # response to become the transcript basis.
        correction_path.unlink()
        update_transcript_status(
            folder, "可接受（纠错稿不完整，回退原始网页转录）", "sample_checked")
    source_path = folder / "来源.md"
    correction_inputs = [
        path for path in (raw_path, transcript_path, source_path)
        if path.exists()
    ]

    contract_required = (
        source_kind == "local_asr" and correction_contract_required(raw)
    )
    force_structured_correction = (
        os.environ.get("FORCE_STRUCTURED_CORRECTION", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    structured_correction = contract_required or force_structured_correction
    if _completeness_blocks_content(source_kind, raw):
        print(
            "[内容][阻断] 新 ASR revision 的语音完整性检查未通过",
            flush=True,
        )
        return False

    correction_ready = (
        _structured_correction_ready(folder, raw)
        if structured_correction
        else (
            source_kind not in ASR_SOURCE_KINDS
            or correction_path.exists()
        )
    )
    transcript_ready = (
        not force
        and _accepted_transcript_status(
            quality_metadata(folder).get("transcript_status"))
        and correction_ready
    )
    with _stage(run_report, "subagent_transcript_correction") as stage:
        if transcript_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "accepted transcript status",
                    "corrected": correction_path.exists(),
                    "structured": structured_correction,
                })
        elif structured_correction:
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
                """读取 transcript.raw.json、原始转录.txt，
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
            _normalized_map, ids_changed = normalize_generated_unit_ids(payload)
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
        ledger_ready = not force and ledger_is_current(folder)
        if ledger_ready:
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
                max_batch_claims=_env_positive_int(
                    "FACT_CHECK_BATCH_CLAIMS", 12),
                max_batch_chars=_env_positive_int(
                    "FACT_CHECK_BATCH_CHARS", 8000),
                concurrency=_env_positive_int(
                    "SUBAGENT_FACT_CHECK_CONCURRENCY", 2),
            )
            if stage is not None:
                stage.metrics.update(metrics)

    with _stage(run_report, "subagent_content_writing") as stage:
        notes_path = folder / "中文完整笔记.md"
        briefing_path = folder / "讲书稿.md"
        summary_path = folder / "summary_map.json"
        # Recheck after upstream repairs: regeneration is not necessarily a
        # semantic change. Legacy prose without input bindings stays conservative.
        alias_errors = public_entity_alias_errors(
            load_json(entities_path),
            notes_path.read_text(encoding="utf-8")
            if notes_path.exists() else "",
            briefing_path.read_text(encoding="utf-8")
            if briefing_path.exists() else "",
        ) if entities_path.exists() else ["canonical_entities.json 缺失"]
        review_path = folder / "ai_review.json"
        review_content_changed = False
        review_rejected = False
        if review_path.exists():
            try:
                previous_review = load_json(review_path)
            except (OSError, ValueError, TypeError):
                previous_review = {}
            expected_files = previous_review.get("reviewed_files", {})
            if previous_review.get("passed") is False:
                review_rejected = True
                if isinstance(expected_files, dict):
                    for path in (notes_path, briefing_path, summary_path):
                        if path.exists():
                            actual = hashlib.sha256(path.read_bytes()).hexdigest()
                            if expected_files.get(path.name) != actual:
                                review_content_changed = True
                                break
        writing_ready = (
            not force
            and not alias_errors
            and (
                review_content_changed
                or (
                    not review_rejected
                    and _writing_artifacts_are_current(
                        folder,
                        require_bound_inputs=not (
                            content_map_ready and entities_ready and ledger_ready),
                    )
                )
            )
        )
        if writing_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "semantic prose inputs unchanged",
                    "outputs": [
                        notes_path.name, briefing_path.name, summary_path.name,
                    ],
                })
        else:
            writing_prompt = _writing_prompt()
            if review_path.exists():
                try:
                    previous_review = load_json(review_path)
                except (OSError, ValueError, TypeError):
                    previous_review = {}
                if previous_review.get("passed") is False:
                    review_issues = previous_review.get("issues") or []
                    writing_prompt += (
                        "\n\n## 上一轮独立审查的修复清单\n"
                        "以下问题必须逐项修复；不能把审查过程写进公开稿。\n"
                        + json.dumps(review_issues, ensure_ascii=False)
                        + "\n"
                    )
            writing_inputs = [
                path for path in (
                    raw_path, transcript_path, correction_path,
                    content_map_path, entities_path, source_path, ledger_path,
                    review_path if review_path.exists() else None)
                if path is not None and path.exists()
            ]
            try:
                run_edit_task(
                    folder,
                    writing_prompt,
                    task_name="content_writing",
                    allowed_files=[notes_path, briefing_path, summary_path],
                    input_files=writing_inputs,
                    required_files=[notes_path, briefing_path, summary_path],
                )
            except SubagentError:
                retry_prompt = (
                    "只完成内容写作，不要解释过程。读取所有输入，直接创建三个文件："
                    "中文完整笔记.md、讲书稿.md、summary_map.json。"
                    "讲书稿按内容地图写成可播中文稿，笔记保留逐项事实归因；"
                    "summary_map 必须是合法 JSON 且覆盖讲书稿全部 ## 章节。"
                    "不要写 Markdown 代码围栏，不要省略任何必需文件。\n\n"
                    + writing_prompt[-12000:]
                )
                run_edit_task(
                    folder,
                    retry_prompt,
                    task_name="content_writing_retry",
                    allowed_files=[notes_path, briefing_path, summary_path],
                    input_files=writing_inputs,
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
        try:
            finalized = finalize_content_package(folder)
        except ContentFinalizationError as exc:
            if "章节数量不一致" not in str(exc):
                raise
            _repair_summary_map_contract(
                folder,
                folder / "讲书稿.md",
                summary_path,
                content_map_path,
                folder / "中文完整笔记.md",
                str(exc),
            )
            finalized = finalize_content_package(folder)
        briefing_text = finalized["briefing"]
        summary_map = finalized["summary_map"]
        normalization_changes = finalized["normalization_changes"]
        notes_text = (folder / "中文完整笔记.md").read_text(encoding="utf-8")
        briefing_text = (folder / "讲书稿.md").read_text(encoding="utf-8")
        if (not writing_ready) or review_content_changed:
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
            summary_map["writing_inputs_version"] = WRITING_INPUTS_VERSION
            summary_map["writing_inputs"] = _writing_input_hashes(folder)
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
        # Entity mismatches block the package; related missions, vehicles and
        # people must never be substituted by a hard-coded prose rewrite.
        if summary_errors and not errors and not entity_alias_errors:
            # A writer can produce valid prose but omit bindings for some
            # claims/details. Give the dedicated structural repair pass the
            # complete deterministic error list before failing the package.
            _repair_summary_map_contract(
                folder,
                folder / "讲书稿.md",
                summary_path,
                content_map_path,
                folder / "中文完整笔记.md",
                "; ".join(summary_errors[:20]),
            )
            summary_map = load_json(summary_path)
            summary_map = enrich_summary_map_evidence(
                summary_map, notes_text, content_map, briefing_text)
            summary_map["transcript_basis"] = _transcript_basis(folder)
            summary_map["writing_inputs_version"] = WRITING_INPUTS_VERSION
            summary_map["writing_inputs"] = _writing_input_hashes(folder)
            save_json(summary_path, summary_map)
            summary_errors = validate_summary_map(
                summary_map, briefing_text, content_map, notes_text)
        if errors or summary_errors or entity_alias_errors:
            all_errors = errors + summary_errors + entity_alias_errors
            if stage is not None:
                stage.fail("; ".join(all_errors[:10]))
            return False
        # Do not spend a TTS lexicon repair call on already-invalid evidence,
        # mapping or entities. Final AI review still validates the full package.
        tts_readiness = _ensure_tts_lexicon_ready(
            folder, folder / "讲书稿.md")
        if stage is not None:
            stage.metrics.update({
                "unit_count": len(content_map.get("units", [])),
                "warning_count": len(warnings),
                "normalization_changes": normalization_changes,
                "tts_lexicon_entries": tts_readiness["entries"],
                "tts_lexicon_repaired": tts_readiness["repaired"],
                "semantic_prose_reused": writing_ready,
            })

    sync_episode_state(folder)
    return True

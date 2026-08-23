"""Subagent-orchestrated content production for a single episode."""
import os
from contextlib import nullcontext
from pathlib import Path

try:
    from atomic_io import atomic_write_text
    from claim_evidence import refine_claim_evidence
    from content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        normalize_summary_claim_ids,
        save_json,
        IMPORTANCE_VALUES,
        transcript_evidence_mode,
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
    from tts import load_tts_lexicon
except ImportError:
    from scripts.atomic_io import atomic_write_text
    from scripts.claim_evidence import refine_claim_evidence
    from scripts.content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        normalize_summary_claim_ids,
        save_json,
        IMPORTANCE_VALUES,
        transcript_evidence_mode,
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
    from scripts.tts import load_tts_lexicon


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


CONTENT_MAP_GENERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": r"^U\d{4,}$"},
                    "topic": {"type": "string", "minLength": 1},
                    "speaker": {"type": "string"},
                    "claims": {"type": "array", "items": {"type": "string"}},
                    "reasoning": {"type": "array", "items": {"type": "string"}},
                    "examples": {"type": "array", "items": {"type": "string"}},
                    "numbers": {"type": "array", "items": {"type": "string"}},
                    "terms": {"type": "array", "items": {"type": "string"}},
                    "timestamps": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "items": {"type": "number"},
                        },
                    },
                    "importance": {"type": "string", "enum": sorted(IMPORTANCE_VALUES)},
                    "status": {
                        "type": "string",
                        "enum": ["included", "condensed", "excluded"],
                    },
                    "notes": {"type": "string"},
                    "evidence": {
                        "type": "object",
                        "properties": {
                            "segment_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "pattern": r"^S\d{4,}$"},
                            },
                        },
                    },
                },
            },
        },
    },
}


def _validate_content_map_stage(payload, transcript):
    """Validate model-authored map fields before claim-evidence model calls."""
    units = payload.get("units")
    if not isinstance(units, list) or not units:
        return ["content_map.json 没有有效 units"]
    transcript_ids = {
        segment.get("id")
        for segment in transcript.get("segments", [])
        if isinstance(segment, dict) and segment.get("id")
    }
    nonempty_ids = {
        segment.get("id")
        for segment in transcript.get("segments", [])
        if isinstance(segment, dict)
        and segment.get("id")
        and str(segment.get("text", "")).strip()
    }
    errors = []
    evidence_mode = transcript_evidence_mode(transcript)
    seen_units = set()
    accounted = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append(f"units[{index}] 必须是对象")
            continue
        unit_id = unit.get("id") or f"units[{index}]"
        if unit_id in seen_units:
            errors.append(f"重复的 unit id: {unit_id}")
        seen_units.add(unit_id)
        status = unit.get("status")
        if status not in {"included", "condensed", "excluded"}:
            errors.append(f"{unit_id}: 未完成或未知 status={status!r}")
        if unit.get("importance") not in IMPORTANCE_VALUES:
            errors.append(f"{unit_id}: importance 无效")
        claims = unit.get("claims")
        if not isinstance(claims, list):
            errors.append(f"{unit_id}: claims 必须是数组")
            claims = []
        if status == "excluded" and claims:
            errors.append(f"{unit_id}: excluded 单元不得生成 claims")
        if status == "excluded" and not str(unit.get("notes", "")).strip():
            errors.append(f"{unit_id}: excluded 单元必须写明原因")
        if (
                status in {"included", "condensed"}
                and unit.get("importance") in {"high", "medium"}
                and not claims):
            errors.append(f"{unit_id}: high/medium 单元必须包含 claims")
        evidence = unit.get("evidence")
        segment_ids = evidence.get("segment_ids") if isinstance(evidence, dict) else None
        if not isinstance(segment_ids, list) or not segment_ids:
            errors.append(f"{unit_id}: evidence.segment_ids 不能为空")
            continue
        unknown = sorted(set(segment_ids) - transcript_ids)
        if unknown:
            errors.append(f"{unit_id}: evidence 引用了未知片段: {unknown}")
        timestamps = unit.get("timestamps")
        if evidence_mode == "timestamp":
            if not isinstance(timestamps, list) or not timestamps:
                errors.append(f"{unit_id}: timestamp evidence 缺少 timestamps")
            else:
                for window in timestamps:
                    if (
                            not isinstance(window, list)
                            or len(window) != 2
                            or window[0] < 0
                            or window[1] < window[0]):
                        errors.append(f"{unit_id}: timestamp 范围无效: {window!r}")
        elif timestamps:
            errors.append(f"{unit_id}: text_anchor evidence 不得伪造 timestamps")
        accounted.update(set(segment_ids) & transcript_ids)
    missing = sorted(nonempty_ids - accounted)
    if missing:
        errors.append(f"content_map 未记账源片段: {missing[:20]}")
    return errors


def _ensure_content_map(folder, title, force=False):
    path = folder / "content_map.json"
    if force or not path.exists():
        init_content_map(
            folder / "transcript.raw.json",
            path,
            title=title,
        )
    return path


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
        return bool(errors or summary_errors or tts_errors)
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

    transcript_ready = (
        not force
        and _accepted_transcript_status(
            quality_metadata(folder).get("transcript_status"))
        and (
            source_kind not in ASR_SOURCE_KINDS
            or correction_path.exists()
        )
    )
    with _stage(run_report, "subagent_transcript_correction") as stage:
        if transcript_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "accepted transcript status",
                    "corrected": correction_path.exists(),
                })
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
            seed = load_json(content_map_path)
            result = run_json_task(
                folder,
                f"""读取 transcript.raw.json、原始转录.txt，以及存在时的 转录_纠错.txt。
根据当前转录把相邻片段整理为完整语义 unit，只返回 schema JSON，不修改文件。

必须遵守：
- unit id 从 U0001 连续递增；
- 完成后的 status 只能是 included、condensed、excluded；
- 每个 unit 的 evidence.segment_ids 至少引用一个真实 Sxxxx；
- transcript.raw.json 的每个非空 segment 必须至少被一个 unit 记账；
- high/medium included 或 condensed 单元必须有 claims；
- excluded 仅用于广告、寒暄、节目操作或无实质内容，claims 必须为空并在 notes 写明原因；
- timestamp evidence 使用真实 timestamps；text_anchor evidence 的 timestamps 为空；
- 不要生成 claim_evidence、哈希或任何转录中不存在的 unit；
- 不要根据常识补写事实。

初始逐片段 map 已写入 content_map.json，只用于分组参考；不要复用其中的 claim_evidence。""",
                CONTENT_MAP_GENERATION_SCHEMA,
                task_name="content_map",
                model=os.environ.get("SUBAGENT_CONTENT_MAP_MODEL", "") or None,
                timeout=1200,
            )
            generated = result.get("payload")
            if not isinstance(generated, dict):
                raise RuntimeError("content_map subagent 输出必须是对象")
            payload = dict(seed)
            payload["units"] = generated.get("units")
            stage_errors = _validate_content_map_stage(payload, raw)
            if stage_errors:
                if stage is not None:
                    stage.fail("; ".join(stage_errors[:10]))
                return False
            payload, raw = enrich_content_map_evidence(payload, raw)
            save_json(content_map_path, payload)
            if stage is not None:
                stage.metrics.update({
                    "unit_count": len(payload["units"]),
                    "structured_output": True,
                })

    with _stage(run_report, "subagent_claim_evidence") as stage:
        if content_map_ready:
            if stage is not None:
                stage.metrics.update({
                    "skipped": True,
                    "reason": "valid content map",
                })
        else:
            metrics = refine_claim_evidence(
                folder,
                model=os.environ.get("SUBAGENT_CLAIM_MODEL", ""),
                effort=os.environ.get("SUBAGENT_CLAIM_EFFORT", "high"),
                max_batch_chars=_env_positive_int(
                    "CLAIM_EVIDENCE_BATCH_CHARS", 35000),
                concurrency=_env_positive_int(
                    "CLAIM_EVIDENCE_CONCURRENCY", 3),
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
如果存在则读取 转录_纠错.txt，并读取已经完成的 content_map.json。

按顺序完成：
1. 先写 中文完整笔记.md，逐项覆盖所有 included high/medium unit 的
claims、numbers 和 examples，保留人物归属、限定条件、时间顺序和推理链。
2. 再写 讲书稿.md，将完整笔记整理为适合中文收听的讲书稿。
3. 最后写 summary_map.json，映射章节标题、unit_ids 和 claim_ids；并显式填写
notes_claim_ids，只能列入中文完整笔记正文实际覆盖的 claim。若仍有 high/medium claim
未覆盖，先补写笔记，再将其加入 notes_claim_ids。

要求：
- 不得编造转录之外的观点；
- 不得遗漏 included high/medium 的 claims、numbers 或 examples；
- 完成前按中文汉字数自检：中文完整笔记必须至少比讲书稿多百分之十五；不足时只能从转录和 content_map 补充证据细节、数字范围、例子、限定条件与推理链，禁止用重复或空话凑字数；
- 中文完整笔记必须逐项覆盖 content_map 的 examples，讲稿出现的证据例子不得只存在于讲稿；
- 不要修改 content_map.json 或任何证据文件；
- 第一个 ## 前写 50–100 字全局导览；
- 每章正文必须包含 420–900 个中文汉字（按汉字计数，不是总字符），禁止超过 1000；
- 章节标题必须准确概括该章 unit，不得用一个话题标题承载无关的后续主题；
- 讲稿必须保留影响理解的事实状态，例如“节目称”“报道称”“仍在洽谈”“这是预测而非已发生结果”；
- 中文完整笔记和讲稿都禁止出现面向内部的审查决策语言，例如“这里不采用”“这里不保留”“本稿未独立核实”“由于口径不同因此删除”；审查理由只写入 JSON 审查产物，不写给听众；
- 即使讨论政策监督也不要使用“审查过程”这一短语，改用“决策程序”“审批流程”或更具体的领域表达；
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
                    content_map_path, source_path)
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
        if errors or summary_errors:
            all_errors = errors + summary_errors
            if stage is not None:
                stage.fail("; ".join(all_errors[:10]))
            return False
        if stage is not None:
            stage.metrics.update({
                "unit_count": len(content_map.get("units", [])),
                "warning_count": len(warnings),
                "normalization_changes": normalization_changes,
                "tts_lexicon_entries": finalized[
                    "tts_lexicon_entries"],
            })

    sync_episode_state(folder)
    return True

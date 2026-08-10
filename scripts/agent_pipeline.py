"""Subagent-orchestrated content production for a single episode."""
import os
from contextlib import nullcontext
from pathlib import Path

try:
    from claim_evidence import refine_claim_evidence
    from content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        save_json,
        validate_content_map,
        validate_summary_map,
    )
    from episode import (
        quality_metadata,
        sync_episode_state,
        update_transcript_status,
    )
    from evidence import ASR_SOURCE_KINDS, effective_source_kind
    from subagent import run_edit_task
except ImportError:
    from scripts.claim_evidence import refine_claim_evidence
    from scripts.content_map import (
        body_sha256,
        enrich_content_map_evidence,
        enrich_summary_map_evidence,
        init_content_map,
        load_json,
        save_json,
        validate_content_map,
        validate_summary_map,
    )
    from scripts.episode import (
        quality_metadata,
        sync_episode_state,
        update_transcript_status,
    )
    from scripts.evidence import ASR_SOURCE_KINDS, effective_source_kind
    from scripts.subagent import run_edit_task


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
        summary_map = load_json(folder / "summary_map.json")
        errors, _warnings = validate_content_map(content_map, transcript)
        summary_errors = validate_summary_map(
            summary_map, briefing_text, content_map, notes_text)
        return bool(errors or summary_errors)
    except (OSError, ValueError, TypeError):
        return True


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
                f"""读取 transcript.raw.json、原始转录.txt 和 来源.md。
逐段检查人名、公司名、数字、单位、专有名词、说话人归属和明显听写错误。
原始转录不可修改。
转录_纠错.txt 只能修复听写、断句、专名和说话人识别错误，必须保持嘉宾实际表达。
如果嘉宾本身陈述了错误事实，不得在纠错稿中静默改成正确事实；应保留原话，交给
content_map、讲稿归因和最终 fact check 处理。
如果发现会影响总结的错误，写入 转录_纠错.txt；否则不要创建该文件。
当前 source_kind={source_kind!r}。
本地 ASR 必须生成纠错稿；第三方文本只有在确实发现问题时生成。""",
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
- 将相邻转录片段合并为有完整语义的 unit；
- 补充 topic、claims、reasoning、examples、numbers、terms；
- 标记 importance 和 status；
- 保留并正确填写 evidence.segment_ids；
- timestamp evidence 必须使用真实时间，text_anchor evidence 不得伪造时间；
- high/medium unit 必须有 claims；
- excluded 必须写明原因；
- 不要写 claim_evidence，后续由独立 subagent 生成；
- 只修改 content_map.json，不修改其他文件。""",
                task_name="content_map",
                allowed_files=[content_map_path],
                input_files=map_inputs,
                required_files=[content_map_path],
            )
            payload = load_json(content_map_path)
            if not isinstance(payload.get("units"), list) or not payload["units"]:
                if stage is not None:
                    stage.fail("content_map.json 没有有效 units")
                return False
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
1. 先写 中文完整笔记.md，覆盖所有 high/medium claims，保留必要的数字、
人物归属、限定条件和推理链。
2. 再写 讲书稿.md，将完整笔记整理为适合中文收听的讲书稿。
3. 最后写 summary_map.json，映射章节标题、unit_ids 和 claim_ids。

要求：
- 不得编造转录之外的观点；
- 不得遗漏 high/medium claims；
- 不要修改 content_map.json 或任何证据文件；
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
        briefing_text = (folder / "讲书稿.md").read_text(encoding="utf-8")
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        summary_map = enrich_summary_map_evidence(
            load_json(summary_path),
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
            })

    sync_episode_state(folder)
    return True

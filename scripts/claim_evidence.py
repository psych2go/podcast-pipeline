"""Use a subagent to refine each content-map claim to minimal source segments."""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from content_map import (
        apply_claim_evidence_mapping,
        enrich_content_map_evidence,
        load_json,
        save_json,
        validate_content_map,
    )
    from run_report import RunReport
    from subagent import run_json_task
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.content_map import (
        apply_claim_evidence_mapping,
        enrich_content_map_evidence,
        load_json,
        save_json,
        validate_content_map,
    )
    from scripts.run_report import RunReport
    from scripts.subagent import run_json_task


CLAIM_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "primary_segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "context_segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "rationale": {"type": "string", "minLength": 10},
                },
                "required": [
                    "claim_id", "primary_segment_ids",
                    "context_segment_ids", "confidence", "rationale",
                ],
            },
        },
    },
    "required": ["claims"],
}


DEFAULT_BATCH_CHARS = 35000
DEFAULT_CONCURRENCY = 3
PROGRESS_FILENAME = "claim_evidence_progress.json"


def _evidence_revision(transcript):
    evidence = transcript.get("evidence", {}) or {}
    meta = transcript.get("meta", {}) or {}
    return (
        evidence.get("revision_sha256")
        or evidence.get("transcript_sha256")
        or meta.get("evidence_revision_sha256")
        or meta.get("transcript_sha256")
    )


def _write_progress(
        folder, transcript, *, target, completed, pending, failed, status):
    atomic_write_json(Path(folder) / PROGRESS_FILENAME, {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "evidence_revision": _evidence_revision(transcript),
        "target_unit_ids": sorted(target),
        "completed_unit_ids": sorted(completed),
        "pending_unit_ids": sorted(pending),
        "failed_unit_ids": sorted(failed),
    })


def _validate_payload_sources(payloads):
    errors = []
    for payload in payloads:
        unit_id = payload.get("unit_id") or "unknown-unit"
        claims = payload.get("claims") or []
        segments = payload.get("segments") or []
        if claims and not segments:
            errors.append(f"{unit_id}: 有 claims 但没有可用 source segments")
        segment_ids = [
            segment.get("id") for segment in segments
            if isinstance(segment, dict) and segment.get("id")
        ]
        if len(segment_ids) != len(set(segment_ids)):
            errors.append(f"{unit_id}: source segments 存在重复 ID")
    if errors:
        raise RuntimeError(
            "claim evidence 输入结构错误（不重试）: "
            + "; ".join(errors[:10]))


def validate_progress(payload, transcript):
    errors = []
    if not isinstance(payload, dict):
        return ["claim evidence progress 必须是对象"]
    if payload.get("schema_version") != 1:
        errors.append("claim evidence progress schema_version 必须是 1")
    if payload.get("status") != "completed":
        errors.append(
            f"claim evidence progress 尚未完成: {payload.get('status')!r}")
    target = set(payload.get("target_unit_ids") or [])
    completed = set(payload.get("completed_unit_ids") or [])
    pending = set(payload.get("pending_unit_ids") or [])
    failed = set(payload.get("failed_unit_ids") or [])
    if target - completed:
        errors.append(
            f"claim evidence progress 缺少完成 unit: {sorted(target - completed)}")
    if pending:
        errors.append(f"claim evidence progress 仍有 pending unit: {sorted(pending)}")
    if failed:
        errors.append(f"claim evidence progress 仍有 failed unit: {sorted(failed)}")
    expected_revision = _evidence_revision(transcript)
    recorded_revision = payload.get("evidence_revision")
    if (
            expected_revision
            and recorded_revision
            and recorded_revision != expected_revision):
        errors.append("claim evidence progress evidence revision 已过期")
    return errors


def deterministic_fallback_mappings(payloads):
    """Build auditable claim mappings when every configured runner is down.

    The fallback distributes claims across the unit's ordered source segments
    instead of copying the complete unit evidence onto every claim. Final AI
    review still verifies semantic correctness before publication.
    """
    mappings = []
    for payload in payloads:
        segments = [
            segment.get("id")
            for segment in payload.get("segments", [])
            if segment.get("id")
        ]
        claims = payload.get("claims", [])
        if not segments and claims:
            raise RuntimeError(
                f"{payload.get('unit_id')}: 无片段可用于确定性证据降级")
        for index, claim in enumerate(claims):
            segment_id = segments[
                min(len(segments) - 1, index * len(segments) // len(claims))
            ]
            mappings.append({
                "claim_id": claim["claim_id"],
                "segment_ids": [segment_id],
                "primary_segment_ids": [segment_id],
                "context_segment_ids": [],
                "confidence": "medium",
                "rationale": (
                    "外部审查服务不可用，按 claim 顺序绑定到该单元的"
                    "对应原始片段，最终发布审查必须再次核对语义。"
                ),
            })
    return mappings


def _has_complete_claim_evidence(unit):
    claims = unit.get("claims", [])
    if not claims:
        return True
    evidence = unit.get("claim_evidence")
    notes = unit.get("claim_evidence_notes")
    if not isinstance(evidence, dict) or not isinstance(notes, dict):
        return False
    for index, _claim in enumerate(claims, start=1):
        key = f"C{index:02d}"
        segment_ids = evidence.get(key)
        note = notes.get(key)
        if not isinstance(segment_ids, list) or not segment_ids:
            return False
        if not isinstance(note, dict):
            return False
        if note.get("confidence") not in {"high", "medium"}:
            return False
        rationale = str(note.get("rationale", "")).strip()
        if len("".join(rationale.split())) < 10:
            return False
    return True


def _prompt(batch):
    batch_json = json.dumps(batch, ensure_ascii=False)
    return f"""请为下面 JSON 中的 claim 建立精确的转录证据。

对每个非空 claims 项返回一条记录：
- claim_id 使用完整格式，例如 U0003-C02。
- primary_segment_ids 只能放直接支持整条 claim 的最小片段，至少一项。
- context_segment_ids 只放归因、限定或背景所需的相邻片段，可为空。
- 两组 ID 只能从该 unit 提供的 segments.id 中选择，不能重复。
- text 是不可改写的 raw evidence；corrected_text 若存在，是同一 segment 的规范化纠错文本。可用 corrected_text 理解 ASR 专名或漏词，但 segment ID 和证据哈希仍绑定 raw evidence，不得声称已听音频。
- 禁止为了省事把整个 unit 的全部片段复制给每条 claim，除非每个片段确实都不可缺少。
- 不要根据常识补证据；转录没有充分支持时 confidence=low。
- rationale 用一句中文说明为什么这些片段足以支持 claim。

必须覆盖所有 claim，不能遗漏或新增 claim。只返回符合 schema 的 JSON，不修改文件。

输入 JSON：
{batch_json}
"""


def _corrected_segment_texts(folder, transcript):
    corrected_path = Path(folder) / "转录_纠错.txt"
    if not corrected_path.exists():
        return {}
    source_segments = [
        segment for segment in transcript.get("segments", [])
        if isinstance(segment, dict)
        and segment.get("id")
        and str(segment.get("text", "")).strip()
    ]
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(
            r"\n\s*\n", corrected_path.read_text(encoding="utf-8"))
        if paragraph.strip()
    ]
    if len(paragraphs) != len(source_segments):
        return {}
    return {
        segment["id"]: paragraph
        for segment, paragraph in zip(source_segments, paragraphs)
    }


def _unit_payloads(
        content_map, transcript, unit_ids=None, skip_complete=False,
        folder=None):
    corrected_by_id = (
        _corrected_segment_texts(folder, transcript) if folder else {})
    segments = {
        segment.get("id"): {
            "id": segment.get("id"),
            "start": segment.get("start"),
            "end": segment.get("end"),
            "synthetic_boundary": segment.get("synthetic_boundary", False),
            "text": segment.get("text", ""),
            "corrected_text": corrected_by_id.get(segment.get("id")),
        }
        for segment in transcript.get("segments", [])
        if segment.get("id")
    }
    payloads = []
    for unit in content_map.get("units", []):
        if unit_ids and unit.get("id") not in unit_ids:
            continue
        if unit.get("status") == "excluded":
            continue
        if skip_complete and _has_complete_claim_evidence(unit):
            continue
        claims = unit.get("claims", [])
        if not claims:
            continue
        unit_id = unit.get("id")
        payloads.append({
            "unit_id": unit_id,
            "topic": unit.get("topic", ""),
            "claims": [
                {"claim_id": f"{unit_id}-C{index:02d}", "text": claim}
                for index, claim in enumerate(claims, start=1)
            ],
            "segments": [
                segments[segment_id]
                for segment_id in unit.get(
                    "evidence", {}).get("segment_ids", [])
                if segment_id in segments
            ],
        })
    return payloads


def _batches(payloads, max_chars):
    batches = []
    current = []
    current_chars = 0
    for payload in payloads:
        payload_chars = len(json.dumps(payload, ensure_ascii=False))
        if current and current_chars + payload_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(payload)
        current_chars += payload_chars
    if current:
        batches.append(current)
    return batches


def _normalize_mapping_roles(mappings):
    normalized = []
    for raw in mappings or []:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        item = dict(raw)
        primary = item.get("primary_segment_ids")
        context = item.get("context_segment_ids")
        if primary is None:
            primary = item.get("segment_ids", [])
        primary = list(dict.fromkeys(primary or []))
        context = [
            segment_id
            for segment_id in dict.fromkeys(context or [])
            if segment_id not in set(primary)
        ]
        item["primary_segment_ids"] = primary
        item["context_segment_ids"] = context
        item["segment_ids"] = primary + context
        normalized.append(item)
    return normalized


def _batch_label(value):
    if isinstance(value, int):
        return f"{value:03d}"
    return str(value).replace("/", "_").replace(" ", "_")


def _run_batch(folder, batch, model, effort, batch_index):
    result = run_json_task(
        folder,
        _prompt(batch) + (
            f"\n本次 claim evidence 任务 effort 要求：{effort}。"
            "只返回符合 schema 的 JSON，不修改任何文件。"
        ),
        CLAIM_EVIDENCE_SCHEMA,
        task_name=f"claim_evidence_{_batch_label(batch_index)}",
        model=model or None,
        timeout=600,
    )
    payload = result["payload"]
    if isinstance(payload, list):
        payload = {"claims": payload}
    if not isinstance(payload, dict):
        raise RuntimeError(
            "claim evidence 返回值必须是对象或 claim 数组")
    payload["claims"] = _normalize_mapping_roles(payload.get("claims", []))
    return payload, result


def _retry_low_confidence_units(
        folder, batch, mappings, model, batch_index):
    low_unit_ids = {
        str(item.get("claim_id", "")).rsplit("-C", 1)[0]
        for item in mappings
        if isinstance(item, dict) and item.get("confidence") == "low"
    }
    if not low_unit_ids:
        return mappings, []
    by_unit = {item["unit_id"]: item for item in batch}
    merged = list(mappings)
    wrappers = []
    for retry_index, unit_id in enumerate(sorted(low_unit_ids), start=1):
        item = by_unit.get(unit_id)
        if item is None:
            raise RuntimeError(
                f"low confidence claim 引用了未知 unit: {unit_id}")
        payload, wrapper = _run_batch(
            folder,
            [item],
            model,
            "max",
            f"{batch_index}_low_{retry_index}",
        )
        retry_mappings = payload.get("claims", [])
        expected_ids = {
            claim["claim_id"] for claim in item.get("claims", [])
        }
        returned_ids = {
            value.get("claim_id")
            for value in retry_mappings if isinstance(value, dict)
        }
        if returned_ids != expected_ids:
            raise RuntimeError(
                f"low confidence 单 unit 复核返回集合不完整: "
                f"unit={unit_id}, expected={sorted(expected_ids)}, "
                f"actual={sorted(returned_ids)}")
        merged = [
            value for value in merged
            if not str(value.get("claim_id", "")).startswith(
                f"{unit_id}-C")
        ]
        merged.extend(retry_mappings)
        wrappers.append(wrapper)
    return merged, wrappers


def refine_claim_evidence(
        folder, model="", effort="high",
        max_batch_chars=DEFAULT_BATCH_CHARS,
        concurrency=DEFAULT_CONCURRENCY, unit_ids=None,
        allow_fallback=False):
    folder = Path(folder).resolve()
    transcript_path = folder / "transcript.raw.json"
    content_map_path = folder / "content_map.json"
    missing = [
        path.name for path in (transcript_path, content_map_path)
        if not path.exists()
    ]
    if missing:
        raise RuntimeError(f"缺少 claim evidence 输入文件: {missing}")

    content_map = load_json(content_map_path)
    transcript = load_json(transcript_path)
    content_map, transcript = enrich_content_map_evidence(
        content_map, transcript)
    selected_ids = set(unit_ids or [])
    target_unit_ids = {
        unit.get("id")
        for unit in content_map.get("units", [])
        if isinstance(unit, dict)
        and unit.get("id")
        and unit.get("status") != "excluded"
        and unit.get("claims")
        and (not selected_ids or unit.get("id") in selected_ids)
    }
    completed_unit_ids = {
        unit.get("id")
        for unit in content_map.get("units", [])
        if isinstance(unit, dict)
        and unit.get("id") in target_unit_ids
        and _has_complete_claim_evidence(unit)
    }
    payloads = _unit_payloads(
        content_map,
        transcript,
        selected_ids,
        skip_complete=True,
        folder=folder,
    )
    pending_unit_ids = {
        payload.get("unit_id") for payload in payloads
        if payload.get("unit_id")
    }
    failed_unit_ids = set()
    try:
        _validate_payload_sources(payloads)
    except RuntimeError:
        _write_progress(
            folder,
            transcript,
            target=target_unit_ids,
            completed=completed_unit_ids,
            pending=pending_unit_ids,
            failed=pending_unit_ids,
            status="invalid_input",
        )
        raise
    save_json(content_map_path, content_map)
    _write_progress(
        folder,
        transcript,
        target=target_unit_ids,
        completed=completed_unit_ids,
        pending=pending_unit_ids,
        failed=failed_unit_ids,
        status="running" if payloads else "completed",
    )
    if not payloads:
        errors, warnings = validate_content_map(content_map, transcript)
        if errors:
            _write_progress(
                folder,
                transcript,
                target=target_unit_ids,
                completed=completed_unit_ids,
                pending=set(),
                failed=target_unit_ids - completed_unit_ids,
                status="invalid_input",
            )
            raise RuntimeError(
                "没有待精炼 claim，但现有 content map 校验失败: "
                + "; ".join(errors[:10]))
        return {
            "claim_count": 0,
            "batch_count": 0,
            "max_batch_chars": max_batch_chars,
            "concurrency": concurrency,
            "warning_count": len(warnings),
            "reported_cost_usd": None,
            "retry_count": 0,
            "duration_ms": 0,
        }
    batches = _batches(payloads, max_batch_chars)
    mappings = []
    wrappers = []
    failures = []
    fallback_claim_count = 0
    recovered_unit_count = 0

    def checkpoint_completed(unit_ids):
        completed_unit_ids.update(unit_ids)
        pending_unit_ids.difference_update(unit_ids)
        failed_unit_ids.difference_update(unit_ids)
        _write_progress(
            folder,
            transcript,
            target=target_unit_ids,
            completed=completed_unit_ids,
            pending=pending_unit_ids,
            failed=failed_unit_ids,
            status="running",
        )

    with ThreadPoolExecutor(
            max_workers=max(1, min(concurrency, len(batches)))) as pool:
        futures = {
            pool.submit(
                _run_batch, folder, batch, model, effort, index):
                (index, batch)
            for index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(futures):
            batch_index, batch = futures[future]
            try:
                payload, wrapper = future.result()
                batch_mappings = payload.get("claims", [])
                batch_mappings, low_retry_wrappers = (
                    _retry_low_confidence_units(
                        folder,
                        batch,
                        batch_mappings,
                        model,
                        batch_index,
                    )
                )
                batch_unit_ids = {
                    item["unit_id"] for item in batch
                }
                content_map, transcript = apply_claim_evidence_mapping(
                    content_map,
                    transcript,
                    batch_mappings,
                    unit_ids=batch_unit_ids,
                )
                save_json(content_map_path, content_map)
                mappings.extend(batch_mappings)
                wrappers.append(wrapper)
                wrappers.extend(low_retry_wrappers)
                checkpoint_completed(batch_unit_ids)
            except Exception as exc:
                if not allow_fallback:
                    if len(batch) > 1:
                        for unit_index, item in enumerate(batch, start=1):
                            try:
                                payload, wrapper = _run_batch(
                                    folder,
                                    [item],
                                    model,
                                    effort,
                                    f"{batch_index}_{unit_index}",
                                )
                                batch_mappings = payload.get("claims", [])
                                batch_mappings, low_retry_wrappers = (
                                    _retry_low_confidence_units(
                                        folder,
                                        [item],
                                        batch_mappings,
                                        model,
                                        f"{batch_index}_{unit_index}",
                                    )
                                )
                                content_map, transcript = (
                                    apply_claim_evidence_mapping(
                                        content_map,
                                        transcript,
                                        batch_mappings,
                                        unit_ids={item["unit_id"]},
                                    )
                                )
                                save_json(content_map_path, content_map)
                                mappings.extend(batch_mappings)
                                wrappers.append(wrapper)
                                wrappers.extend(low_retry_wrappers)
                                checkpoint_completed({item["unit_id"]})
                                recovered_unit_count += 1
                            except Exception as unit_exc:
                                failed_unit_ids.add(item["unit_id"])
                                failures.append(RuntimeError(
                                    "strict mode forbids fallback claim "
                                    f"evidence for {item['unit_id']}: "
                                    f"{unit_exc}"
                                ))
                    else:
                        failed_unit_ids.update(
                            item.get("unit_id") for item in batch
                            if item.get("unit_id"))
                        failures.append(RuntimeError(
                            "strict mode forbids fallback claim evidence: "
                            f"{exc}"
                        ))
                    continue
                try:
                    batch_mappings = deterministic_fallback_mappings(batch)
                    batch_unit_ids = {
                        item["unit_id"] for item in batch
                    }
                    content_map, transcript = apply_claim_evidence_mapping(
                        content_map,
                        transcript,
                        batch_mappings,
                        unit_ids=batch_unit_ids,
                    )
                    save_json(content_map_path, content_map)
                    mappings.extend(batch_mappings)
                    fallback_claim_count += len(batch_mappings)
                    checkpoint_completed(batch_unit_ids)
                    wrappers.append({
                        "retry_count": 0,
                        "duration_ms": 0,
                        "fallback_error": str(exc),
                    })
                    print(
                        "[claim evidence][降级] subagent 不可用，"
                        f"已为 {len(batch_mappings)} 条 claim 写入"
                        "确定性片段映射；最终 AI 审查仍会阻断错误发布",
                        flush=True,
                    )
                except Exception as fallback_exc:
                    failed_unit_ids.update(
                        item.get("unit_id") for item in batch
                        if item.get("unit_id"))
                    failures.append(fallback_exc)
    if failures:
        _write_progress(
            folder,
            transcript,
            target=target_unit_ids,
            completed=completed_unit_ids,
            pending=pending_unit_ids,
            failed=failed_unit_ids,
            status="partial",
        )
        completed_units = sum(
            _has_complete_claim_evidence(unit)
            for unit in content_map.get("units", [])
            if unit.get("claims")
        )
        raise RuntimeError(
            f"claim evidence 批次失败；已保存 {completed_units} 个 unit，"
            "可直接重跑继续: "
            f"{failures[0]}"
        ) from failures[0]

    expected_claim_ids = {
        claim["claim_id"]
        for payload in payloads
        for claim in payload["claims"]
    }
    returned_claim_ids = {
        item.get("claim_id") for item in mappings if isinstance(item, dict)
    }
    missing_claim_ids = sorted(expected_claim_ids - returned_claim_ids)
    extra_claim_ids = sorted(returned_claim_ids - expected_claim_ids)
    if missing_claim_ids or extra_claim_ids:
        raise RuntimeError(
            "claim evidence 返回集合不完整: "
            f"missing={missing_claim_ids}, extra={extra_claim_ids}")

    errors, warnings = validate_content_map(content_map, transcript)
    if errors:
        failed_from_validation = set(re.findall(
            r"\bU\d{4,}\b", " ".join(errors))) & target_unit_ids
        if not failed_from_validation:
            failed_from_validation = set(target_unit_ids)
        _write_progress(
            folder,
            transcript,
            target=target_unit_ids,
            completed=target_unit_ids - failed_from_validation,
            pending=failed_from_validation,
            failed=failed_from_validation,
            status="invalid_result",
        )
        raise RuntimeError(
            "claim evidence 精炼后校验失败: " + "; ".join(errors[:10]))
    content_map["claim_evidence_refiner"] = {
        "command": (
            "codex-subagent+deterministic-fallback"
            if fallback_claim_count else "codex-subagent"
        ),
        "model": model or "default",
        "effort": effort,
        "batch_count": len(batches),
        "max_batch_chars": max_batch_chars,
        "concurrency": concurrency,
        "reported_cost_usd": None,
        "retry_count": sum(
            wrapper.get("retry_count", 0) for wrapper in wrappers),
        "duration_ms": sum(
            wrapper.get("duration_ms") or 0 for wrapper in wrappers),
        "fallback_claim_count": fallback_claim_count,
        "recovered_unit_count": recovered_unit_count,
    }
    save_json(content_map_path, content_map)
    _write_progress(
        folder,
        transcript,
        target=target_unit_ids,
        completed=target_unit_ids,
        pending=set(),
        failed=set(),
        status="completed",
    )
    return {
        "claim_count": len(mappings),
        "batch_count": len(batches),
        "max_batch_chars": max_batch_chars,
        "concurrency": concurrency,
        "warning_count": len(warnings),
        "reported_cost_usd": content_map[
            "claim_evidence_refiner"]["reported_cost_usd"],
        "retry_count": content_map[
            "claim_evidence_refiner"]["retry_count"],
        "duration_ms": content_map[
            "claim_evidence_refiner"]["duration_ms"],
        "fallback_claim_count": fallback_claim_count,
        "recovered_unit_count": recovered_unit_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description="使用 subagent 为 content_map 生成 claim 级最小转录证据")
    parser.add_argument("folder")
    parser.add_argument(
        "--model", default=os.environ.get("SUBAGENT_CLAIM_MODEL", ""))
    parser.add_argument(
        "--effort", default=os.environ.get("SUBAGENT_CLAIM_EFFORT", "high"))
    parser.add_argument(
        "--batch-chars",
        type=int,
        default=int(os.environ.get(
            "CLAIM_EVIDENCE_BATCH_CHARS", DEFAULT_BATCH_CHARS)),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get(
            "CLAIM_EVIDENCE_CONCURRENCY", DEFAULT_CONCURRENCY)),
    )
    parser.add_argument(
        "--unit",
        action="append",
        default=[],
        help="只精炼指定 unit，可重复传入；默认处理全部 unit",
    )
    parser.add_argument(
        "--allow-degraded-evidence",
        action="store_true",
        help=(
            "显式允许 subagent 不可用时写入低保证的确定性映射；"
            "严格新单集默认禁止"
        ),
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    report = RunReport(folder, "claim_evidence.refine", {
        "model": args.model,
        "effort": args.effort,
    })
    try:
        with report.stage("refine_claim_evidence") as stage:
            metrics = refine_claim_evidence(
                folder,
                model=args.model,
                effort=args.effort,
                max_batch_chars=args.batch_chars,
                concurrency=args.concurrency,
                unit_ids=args.unit,
                allow_fallback=args.allow_degraded_evidence,
            )
            stage.metrics.update(metrics)
    except BaseException as exc:
        report.finish(False, exc)
        raise
    report.finish(True, metrics=metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

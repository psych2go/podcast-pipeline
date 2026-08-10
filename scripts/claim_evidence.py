"""Use a subagent to refine each content-map claim to minimal source segments."""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
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
                    "segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "rationale": {"type": "string", "minLength": 10},
                },
                "required": [
                    "claim_id", "segment_ids", "confidence", "rationale",
                ],
            },
        },
    },
    "required": ["claims"],
}


DEFAULT_BATCH_CHARS = 35000
DEFAULT_CONCURRENCY = 3


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
- segment_ids 只能从该 unit 提供的 segments.id 中选择。
- 选择能够直接支持整条 claim 的最小片段集合。
- 人物归因、限定条件、数字或因果关系需要相邻上下文时可以多选片段。
- 禁止为了省事把整个 unit 的全部片段复制给每条 claim，除非每个片段确实都不可缺少。
- 不要根据常识补证据；转录没有充分支持时 confidence=low。
- rationale 用一句中文说明为什么这些片段足以支持 claim。

必须覆盖所有 claim，不能遗漏或新增 claim。只返回符合 schema 的 JSON，不修改文件。

输入 JSON：
{batch_json}
"""


def _unit_payloads(
        content_map, transcript, unit_ids=None, skip_complete=False):
    segments = {
        segment.get("id"): {
            "id": segment.get("id"),
            "start": segment.get("start"),
            "end": segment.get("end"),
            "synthetic_boundary": segment.get("synthetic_boundary", False),
            "text": segment.get("text", ""),
        }
        for segment in transcript.get("segments", [])
        if segment.get("id")
    }
    payloads = []
    for unit in content_map.get("units", []):
        if unit_ids and unit.get("id") not in unit_ids:
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


def _run_batch(folder, batch, model, effort, batch_index):
    result = run_json_task(
        folder,
        _prompt(batch) + (
            f"\n本次 claim evidence 任务 effort 要求：{effort}。"
            "只返回符合 schema 的 JSON，不修改任何文件。"
        ),
        CLAIM_EVIDENCE_SCHEMA,
        task_name=f"claim_evidence_{batch_index:03d}",
        model=model or None,
        timeout=600,
    )
    payload = result["payload"]
    if isinstance(payload, list):
        payload = {"claims": payload}
    if not isinstance(payload, dict):
        raise RuntimeError(
            "claim evidence 返回值必须是对象或 claim 数组")
    return payload, result


def refine_claim_evidence(
        folder, model="sonnet", effort="high",
        max_batch_chars=DEFAULT_BATCH_CHARS,
        concurrency=DEFAULT_CONCURRENCY, unit_ids=None):
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
    save_json(content_map_path, content_map)
    payloads = _unit_payloads(
        content_map,
        transcript,
        set(unit_ids or []),
        skip_complete=True,
    )
    if not payloads:
        errors, warnings = validate_content_map(content_map, transcript)
        if errors:
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
    with ThreadPoolExecutor(
            max_workers=max(1, min(concurrency, len(batches)))) as pool:
        futures = {
            pool.submit(
                _run_batch, folder, batch, model, effort, index): batch
            for index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                payload, wrapper = future.result()
                batch_mappings = payload.get("claims", [])
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
            except Exception as exc:
                failures.append(exc)
    if failures:
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
        raise RuntimeError(
            "claim evidence 精炼后校验失败: " + "; ".join(errors[:10]))
    content_map["claim_evidence_refiner"] = {
        "command": "codex-subagent",
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
    }
    save_json(content_map_path, content_map)
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

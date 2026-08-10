"""Use Claude CLI to refine each content-map claim to minimal source segments."""
import argparse
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from content_map import (
        apply_claim_evidence_mapping,
        load_json,
        save_json,
        validate_content_map,
    )
    from run_report import RunReport
except ImportError:
    from scripts.content_map import (
        apply_claim_evidence_mapping,
        load_json,
        save_json,
        validate_content_map,
    )
    from scripts.run_report import RunReport


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
                    "rationale": {"type": "string"},
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


def _parse_claude_output(stdout):
    wrapper = json.loads(stdout)
    payload = wrapper.get("structured_output")
    if isinstance(payload, dict):
        return payload, wrapper
    raw = wrapper.get("result", "")
    return json.loads(raw), wrapper


def _unit_payloads(content_map, transcript, unit_ids=None):
    segments = {
        segment.get("id"): {
            "id": segment.get("id"),
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text": segment.get("text", ""),
        }
        for segment in transcript.get("segments", [])
        if segment.get("id")
    }
    payloads = []
    for unit in content_map.get("units", []):
        if unit_ids and unit.get("id") not in unit_ids:
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


def _run_batch(claude, batch, model, effort):
    cmd = [
        claude,
        "--safe-mode",
        "-p",
        _prompt(batch),
        "--effort",
        effort,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(CLAIM_EVIDENCE_SCHEMA, ensure_ascii=False),
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--allowedTools",
        "",
    ]
    if model:
        cmd.extend(["--model", model])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude claim evidence 批次失败: {result.stderr[-1000:]}")
    return _parse_claude_output(result.stdout)


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

    claude = shutil.which(os.environ.get("AI_REVIEW_COMMAND", "claude"))
    if not claude:
        raise RuntimeError("找不到 claude CLI，无法精炼 claim evidence")
    content_map = load_json(content_map_path)
    transcript = load_json(transcript_path)
    payloads = _unit_payloads(
        content_map, transcript, set(unit_ids or []))
    if not payloads:
        raise RuntimeError("没有可精炼的 claim；请检查 --unit 参数")
    batches = _batches(payloads, max_batch_chars)
    mappings = []
    wrappers = []
    with ThreadPoolExecutor(
            max_workers=max(1, min(concurrency, len(batches)))) as pool:
        futures = {
            pool.submit(_run_batch, claude, batch, model, effort): batch
            for batch in batches
        }
        for future in as_completed(futures):
            payload, wrapper = future.result()
            mappings.extend(payload.get("claims", []))
            wrappers.append(wrapper)

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

    content_map, transcript = apply_claim_evidence_mapping(
        content_map,
        transcript,
        mappings,
    )
    errors, warnings = validate_content_map(content_map, transcript)
    if errors:
        raise RuntimeError(
            "claim evidence 精炼后校验失败: " + "; ".join(errors[:10]))
    content_map["claim_evidence_refiner"] = {
        "command": "claude",
        "model": model or "default",
        "effort": effort,
        "batch_count": len(batches),
        "max_batch_chars": max_batch_chars,
        "concurrency": concurrency,
        "reported_cost_usd": sum(
            wrapper.get("total_cost_usd") or 0 for wrapper in wrappers),
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
        "duration_ms": content_map[
            "claim_evidence_refiner"]["duration_ms"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="使用 Claude 为 content_map 生成 claim 级最小转录证据")
    parser.add_argument("folder")
    parser.add_argument(
        "--model", default=os.environ.get("CLAIM_EVIDENCE_MODEL", "sonnet"))
    parser.add_argument(
        "--effort", default=os.environ.get("CLAIM_EVIDENCE_EFFORT", "high"))
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

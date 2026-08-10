"""使用 subagent 对单集转录、内容台账和中文讲稿执行全自动 AI 审查。"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from evidence import ASR_SOURCE_KINDS, effective_source_kind
    from run_report import RunReport
    from subagent import run_json_task
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.evidence import ASR_SOURCE_KINDS, effective_source_kind
    from scripts.run_report import RunReport
    from scripts.subagent import run_json_task

REVIEW_FILES = (
    "episode.json",
    "transcript.raw.json",
    "原始转录.txt",
    "content_map.json",
    "中文完整笔记.md",
    "讲书稿.md",
    "summary_map.json",
    "来源.md",
)
OPTIONAL_REVIEW_FILES = ("转录_纠错.txt", "tts_lexicon.json")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
        "transcript_quality": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "raw_score": {
                    "type": "number", "minimum": 0, "maximum": 100,
                },
                "corrected_score": {
                    "type": "number", "minimum": 0, "maximum": 100,
                },
                "accuracy_basis": {
                    "type": "string",
                    "enum": [
                        "reference_wer",
                        "sample_audit",
                        "semantic_review_only",
                    ],
                },
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "score", "issues"],
        },
        "coverage": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "missing_topics": {"type": "array", "items": {"type": "string"}},
                "overcompressed_topics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "score", "missing_topics", "overcompressed_topics"],
        },
        "factuality": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "score", "issues"],
        },
        "numbers": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "issues"],
        },
        "attribution": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "issues"],
        },
        "tts": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "issues"],
        },
        "publish": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "issues"],
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "category": {"type": "string"},
                    "file": {"type": "string"},
                    "statement": {"type": "string"},
                    "source_evidence": {"type": "string"},
                    "evidence_type": {
                        "type": "string",
                        "enum": ["transcript", "web", "mixed", "process"],
                    },
                    "evidence_segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "checked_at": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": [
                    "severity", "category", "file", "statement",
                    "source_evidence", "evidence_type",
                    "evidence_segment_ids", "source_urls", "checked_at",
                    "recommendation",
                ],
            },
        },
        "fact_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["supported", "qualified", "unsupported"],
                    },
                    "publication_status": {
                        "type": "string",
                        "enum": [
                            "used_as_fact",
                            "attributed_or_qualified",
                            "excluded",
                        ],
                    },
                    "evidence_segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "checked_at": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "claim", "verdict", "publication_status",
                    "evidence_segment_ids",
                    "source_urls", "checked_at", "notes",
                ],
            },
        },
    },
    "required": [
        "passed", "summary", "transcript_quality", "coverage", "factuality",
        "numbers", "attribution", "tts", "publish", "issues", "fact_checks",
    ],
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reviewed_hashes(folder):
    folder = Path(folder)
    return {
        name: sha256(folder / name)
        for name in (*REVIEW_FILES, *OPTIONAL_REVIEW_FILES)
        if (folder / name).exists()
    }


def _replace_provenance_wording(value):
    if isinstance(value, str):
        return value.replace("官方字幕", "历史 ASR 转录")
    if isinstance(value, list):
        return [_replace_provenance_wording(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_provenance_wording(item)
            for key, item in value.items()
        }
    return value


def rebind_provenance_review(folder, output=None):
    """Refresh a passed review after a proven metadata-only migration."""
    folder = Path(folder)
    output = Path(output) if output else folder / "ai_review.json"
    if not output.exists():
        raise RuntimeError("缺少 ai_review.json，不能执行 provenance rebind")
    review = json.loads(output.read_text(encoding="utf-8"))
    if not review.get("passed", False):
        raise RuntimeError("只有已通过的 AI 审查可以执行 provenance rebind")

    expected = review.get("reviewed_files", {})
    current = reviewed_hashes(folder)
    changed = sorted(
        name for name in set(expected) | set(current)
        if expected.get(name) != current.get(name)
    )
    if not changed:
        return review
    allowed = {"episode.json", "来源.md", "transcript.raw.json"}
    unexpected = sorted(set(changed) - allowed)
    if unexpected:
        raise RuntimeError(
            f"存在语义文件变化，必须重新 AI 审查: {unexpected}")

    raw = json.loads(
        (folder / "transcript.raw.json").read_text(encoding="utf-8"))
    migration = raw.get("provenance", {}).get("migration", {})
    if migration.get("kind") != "metadata_only":
        raise RuntimeError("transcript.raw.json 缺少 metadata_only 迁移证明")
    if migration.get("previous_raw_sha256") != expected.get(
            "transcript.raw.json"):
        raise RuntimeError("迁移前 transcript.raw.json 哈希与原审查不匹配")

    source_kind = effective_source_kind(folder, raw)
    if source_kind == "legacy_asr":
        review = _replace_provenance_wording(review)
    if source_kind in ASR_SOURCE_KINDS:
        transcript_quality = review.setdefault("transcript_quality", {})
        transcript_quality.setdefault(
            "corrected_score", transcript_quality.get("score"))
        transcript_quality.setdefault(
            "accuracy_basis", "semantic_review_only")
    review["provenance_rebind"] = {
        "method": "metadata_only",
        "changed_files": changed,
        "rebound_at": datetime.now(timezone.utc).isoformat(),
        "semantic_review_reused": True,
    }
    update_source_status(folder, True)
    review["reviewed_files"] = reviewed_hashes(folder)
    atomic_write_json(output, review)
    return review


def _prompt(folder):
    folder = Path(folder).resolve()
    return f"""你是播客流水线的最终发布审查员。请以 max effort 审查目录：
{folder}

必须读取：episode.json、来源.md、transcript.raw.json、原始转录.txt、content_map.json、中文完整笔记.md、讲书稿.md、summary_map.json；如果存在，还必须读取 转录_纠错.txt 和 tts_lexicon.json。

审查目标：无需人工复核也能直接发布。请执行：
1. 抽查所有章节和对应时间片，检查脑补、漏点、人物归属和逻辑链。
2. 审查 content_map 中每个 claims/numbers/examples 是否被完整笔记或讲稿正确覆盖。
   content_map v3 的 evidence.segment_ids、claim_evidence、claim_evidence_sha256 和 source_sha256 是证据锚点；逐条 claim 必须引用对应的 Sxxxx 片段，不能只相信 summary_map 自报。
3. 对金额、百分比、倍数、年份、功率/能量单位、公司规模等高风险数字进行联网交叉验证。节目观点可以保留，但事实错误必须指出。
4. 检查第三方转录中的 ASR 错词、专有名词和明显异常数字。
   如果存在 转录_纠错.txt，它是供下游使用的规范化转录：transcript_quality 应同时评价原始稿的可追溯性和纠错稿是否已消除会影响总结的错误，不要因为保留了不可改写的原始证据而重复扣分。
   对 local_asr 或 legacy_asr，transcript_quality 另外填写 raw_score、
   corrected_score 和 accuracy_basis。没有人工标准逐字稿时不得暗示计算过 WER，
   accuracy_basis 应为 sample_audit 或 semantic_review_only；score 继续表示下游实际
   使用的纠错后综合质量。
   转录纠错只能修复听写、断句、专名和说话人识别错误。节目嘉宾本身说错的事实必须
   保留原话，并在 fact_checks 或稿件归因中纠正，不能把原始发言改写成编辑部事实。
5. 检查中文完整笔记是否确实比精编讲稿更完整；若更短或漏掉重要细节，不能通过。
6. 检查讲稿是否适合中文 TTS：阿拉伯数字、英文缩写、专有名词、难读符号和可能误读的混合表达。
   如果存在 tts_lexicon.json，还要检查替换是否会改变原意、误替换子串或引入错误读音。
7. 检查 summary_map 是否真实反映讲稿，而不是只自报 unit IDs。
   同时检查 notes_claim_ids 与中文完整笔记正文是否真实对应。
   哈希规范化规则与 scripts/content_map.py 一致：讲稿按换行后紧跟“## ”切章，
   body_sha256 只计算标题行之后的章节正文，正文先 strip 再做 UTF-8 SHA-256；
   notes_sha256 对中文完整笔记全文先 strip 再做 UTF-8 SHA-256。请按此规则复核，
   不要使用原始文件字节、标题行或其他自行推测的切章方式。
8. publish 只评价上述必读内容文件本身是否已达到发布标准。
   不要读取、检查或评价 HTML、MP3、tts_manifest.json、quality_report.json、publish_report.json、
   文件修改时间、R2、Cloudflare Pages 或线上页面；这些机械产物的新鲜度和可用性由流水线的
   确定性预检负责。不得因为这些产物尚未生成、较旧或未上传而令 publish.passed=false，
   也不得为此创建 issue。只要必读内容文件达到发布标准，publish.passed 必须为 true。

证据输出要求：
- 每个 issue 都必须填写 evidence_type、evidence_segment_ids、source_urls 和 checked_at。
- 联网事实核查必须把最终采用的网页 URL 写入 source_urls；只引用转录时 source_urls 可为空。
- 对讲稿中实际采用的高风险数字和动态事实写入 fact_checks，即使结论为 supported 也要保留证据。
- fact_checks.publication_status 必须区分：作为事实采用 used_as_fact、仅按节目观点归因或加限定 attributed_or_qualified、未进入发布稿 excluded。

判定规则：
- 任何 critical/high 问题存在时 passed=false。
- factuality、numbers、attribution、transcript_quality 任一不通过时 passed=false。
- transcript_quality、coverage、factuality 使用百分制；任何一项低于九十分时 passed=false。
- 重要事实只能在有转录依据或可靠网页依据时通过。
- 对动态数字注明“节目播出时/节目称”，不能把不断变化的数值写成永久事实。
- 不要修改任何文件，只返回符合 schema 的 JSON。
"""


def run_ai_review(folder, output=None, model=None, effort="max"):
    folder = Path(folder).resolve()
    missing = [name for name in REVIEW_FILES if not (folder / name).exists()]
    if missing:
        raise RuntimeError(f"缺少 AI 审查输入文件: {missing}")
    print(
        f"[AI审查] subagent model={model or 'default'} "
        f"effort={effort} folder={folder.name}",
        file=sys.stderr,
        flush=True,
    )
    schema_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False)
    schema_path = Path(schema_file.name)
    try:
        json.dump(REVIEW_SCHEMA, schema_file, ensure_ascii=False)
        schema_file.close()
        result = run_json_task(
            folder,
            _prompt(folder) + (
                f"\n本次审查 effort 要求：{effort}。"
                "只返回符合 schema 的 JSON，不修改任何文件。"
            ),
            schema_path,
            task_name="ai_review",
            enable_search=True,
            model=model or None,
            timeout=1800,
        )
        review = result["payload"]
    finally:
        try:
            schema_file.close()
        finally:
            schema_path.unlink(missing_ok=True)
    review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    review["reviewer"] = {
        "command": result.get("command", "codex exec"),
        "effort": effort,
        "model": model or "codex-default",
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": None,
        "reported_cost_usd": None,
        "usage": {},
        "retry_count": result.get("retry_count", 0),
    }
    review["reviewed_files"] = reviewed_hashes(folder)
    output = Path(output) if output else folder / "ai_review.json"
    atomic_write_json(output, review)
    return review


def update_source_status(folder, passed):
    try:
        from episode import update_review_status
    except ImportError:
        from scripts.episode import update_review_status
    update_review_status(folder, passed)


def review_episode(
        folder, output=None, model="", effort="max", run_report=None):
    """完成审查、更新来源状态，并以更新后的文件哈希写入最终审查记录。"""
    folder = Path(folder)
    output = Path(output) if output else folder / "ai_review.json"
    owns_report = run_report is None
    report = run_report or RunReport(folder, "ai_review", {
        "model": model,
        "effort": effort,
    })
    try:
        with report.stage("ai_review") as stage:
            review = run_ai_review(folder, output, model, effort)
            update_source_status(folder, review.get("passed", False))
            review["reviewed_files"] = reviewed_hashes(folder)
            atomic_write_json(output, review)
            reviewer = review.get("reviewer", {})
            stage.metrics.update({
                "passed": bool(review.get("passed")),
                "issue_count": len(review.get("issues", [])),
                "fact_check_count": len(review.get("fact_checks", [])),
                "reported_cost_usd": reviewer.get("reported_cost_usd"),
                "duration_api_ms": reviewer.get("duration_api_ms"),
                "retry_count": reviewer.get("retry_count", 0),
                "usage": reviewer.get("usage"),
            })
            if not review.get("passed"):
                stage.fail("AI review did not pass")
    except Exception as exc:
        if owns_report:
            report.finish(False, exc)
        raise
    if owns_report:
        report.finish(
            bool(review.get("passed")),
            None if review.get("passed") else "AI review did not pass",
        )
    return review


def main():
    parser = argparse.ArgumentParser(description="使用 subagent 自动审查播客最终产物")
    parser.add_argument("folder")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--model", default=os.environ.get("SUBAGENT_REVIEW_MODEL", ""))
    parser.add_argument(
        "--effort", default=os.environ.get("SUBAGENT_REVIEW_EFFORT", "max"))
    parser.add_argument(
        "--rebind-provenance",
        action="store_true",
        help="仅在 metadata_only provenance 迁移后刷新原审查哈希",
    )
    args = parser.parse_args()
    if args.rebind_provenance:
        review = rebind_provenance_review(args.folder, args.out)
    else:
        review = review_episode(
            args.folder, args.out, model=args.model, effort=args.effort)
    print(json.dumps(review, ensure_ascii=False, indent=2))
    return 0 if review.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

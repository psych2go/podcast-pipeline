"""使用 Claude CLI 对单集转录、内容台账和中文讲稿执行全自动 AI 审查。"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from run_report import RunReport
except ImportError:
    from scripts.run_report import RunReport

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
                    "claim", "verdict", "evidence_segment_ids",
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


def _prompt(folder):
    folder = Path(folder).resolve()
    return f"""你是播客流水线的最终发布审查员。请以 max effort 审查目录：
{folder}

必须读取：episode.json、来源.md、transcript.raw.json、原始转录.txt、content_map.json、中文完整笔记.md、讲书稿.md、summary_map.json；如果存在，还必须读取 转录_纠错.txt 和 tts_lexicon.json。

审查目标：无需人工复核也能直接发布。请执行：
1. 抽查所有章节和对应时间片，检查脑补、漏点、人物归属和逻辑链。
2. 审查 content_map 中每个 claims/numbers/examples 是否被完整笔记或讲稿正确覆盖。
   content_map v2 的 evidence.segment_ids、claim_evidence 和 source_sha256 是证据锚点；逐条 claim 必须引用对应的 Sxxxx 片段，不能只相信 summary_map 自报。
3. 对金额、百分比、倍数、年份、功率/能量单位、公司规模等高风险数字进行联网交叉验证。节目观点可以保留，但事实错误必须指出。
4. 检查第三方转录中的 ASR 错词、专有名词和明显异常数字。
   如果存在 转录_纠错.txt，它是供下游使用的规范化转录：transcript_quality 应同时评价原始稿的可追溯性和纠错稿是否已消除会影响总结的错误，不要因为保留了不可改写的原始证据而重复扣分。
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
    claude = shutil.which(os.environ.get("AI_REVIEW_COMMAND", "claude"))
    if not claude:
        raise RuntimeError("找不到 claude CLI，无法执行自动 AI 审查")
    cmd = [
        claude, "--safe-mode", "-p", _prompt(folder), "--effort", effort,
        "--output-format", "json",
        "--json-schema", json.dumps(REVIEW_SCHEMA, ensure_ascii=False),
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--allowedTools", "Read,Grep,WebSearch,WebFetch",
        "--add-dir", str(folder),
    ]
    if model:
        cmd.extend(["--model", model])
    print(
        f"[AI审查] model={model or 'default'} effort={effort} folder={folder.name}",
        file=sys.stderr,
        flush=True,
    )
    try:
        result = subprocess.run(
            cmd, cwd=folder.parent.parent, capture_output=True, text=True,
            timeout=1800, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Claude AI 审查超过 30 分钟，已中止") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Claude AI 审查失败: {result.stderr[-1000:]}")
    try:
        wrapper = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Claude AI 审查返回了无效 JSON: {result.stdout[-1000:]}"
        ) from exc
    review = wrapper.get("structured_output")
    if not isinstance(review, dict):
        raw = wrapper.get("result", "")
        review = json.loads(raw)
    review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    review["reviewer"] = {
        "command": "claude",
        "effort": effort,
        "model": model or "default",
        "duration_ms": wrapper.get("duration_ms"),
        "duration_api_ms": wrapper.get("duration_api_ms"),
        "reported_cost_usd": wrapper.get("total_cost_usd"),
        "usage": wrapper.get("usage"),
    }
    review["reviewed_files"] = reviewed_hashes(folder)
    output = Path(output) if output else folder / "ai_review.json"
    output.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return review


def update_source_status(folder, passed):
    try:
        from episode import update_review_status
    except ImportError:
        from scripts.episode import update_review_status
    update_review_status(folder, passed)


def review_episode(
        folder, output=None, model="opus", effort="max", run_report=None):
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
            output.write_text(
                json.dumps(review, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            reviewer = review.get("reviewer", {})
            stage.metrics.update({
                "passed": bool(review.get("passed")),
                "issue_count": len(review.get("issues", [])),
                "fact_check_count": len(review.get("fact_checks", [])),
                "reported_cost_usd": reviewer.get("reported_cost_usd"),
                "duration_api_ms": reviewer.get("duration_api_ms"),
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
    parser = argparse.ArgumentParser(description="使用 Claude CLI 自动审查播客最终产物")
    parser.add_argument("folder")
    parser.add_argument("--out", default=None)
    parser.add_argument("--model", default=os.environ.get("AI_REVIEW_MODEL", "opus"))
    parser.add_argument("--effort", default=os.environ.get("AI_REVIEW_EFFORT", "max"))
    args = parser.parse_args()
    review = review_episode(
        args.folder, args.out, model=args.model, effort=args.effort)
    print(json.dumps(review, ensure_ascii=False, indent=2))
    return 0 if review.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

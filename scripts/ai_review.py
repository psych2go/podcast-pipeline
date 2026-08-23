"""使用 subagent 对单集转录、内容台账和中文讲稿执行全自动 AI 审查。"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    from hashing import sha256_file as sha256
except ImportError:
    from scripts.hashing import sha256_file as sha256

try:
    from atomic_io import atomic_write_json
    from evidence import ASR_SOURCE_KINDS, effective_source_kind
    from run_report import RunReport
    from source_relevance import refresh_source_relevance_cache
    from subagent import run_json_task
    from fact_check_cache import (
        CACHE_FILENAME, build_cache_context, update_cache_from_review)
    from claim_taxonomy import (
        AI_REVIEW_SCHEMA_VERSION,
        ASSERTION_TYPES,
        CLAIM_ORIGINS,
        LEGACY_CLAIM_TYPES,
        normalize_review_fact_checks,
        validate_review_fact_checks,
        PUBLICATION_STATUSES,
        RISK_DOMAINS,
        SPEAKER_ROLES,
        VERDICTS,
        VERIFICATION_MODES,
    )
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.evidence import ASR_SOURCE_KINDS, effective_source_kind
    from scripts.run_report import RunReport
    from scripts.source_relevance import refresh_source_relevance_cache
    from scripts.subagent import run_json_task
    from scripts.fact_check_cache import (
        CACHE_FILENAME, build_cache_context, update_cache_from_review)
    from scripts.claim_taxonomy import (
        AI_REVIEW_SCHEMA_VERSION,
        ASSERTION_TYPES,
        CLAIM_ORIGINS,
        LEGACY_CLAIM_TYPES,
        normalize_review_fact_checks,
        validate_review_fact_checks,
        PUBLICATION_STATUSES,
        RISK_DOMAINS,
        SPEAKER_ROLES,
        VERDICTS,
        VERIFICATION_MODES,
    )

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
OPTIONAL_REVIEW_FILES = (
    "转录_纠错.txt", "tts_lexicon.json", "editorial_corrections.json",
    "canonical_entities.json", "source_relevance_cache.json")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {
            "type": "integer",
            "const": AI_REVIEW_SCHEMA_VERSION,
        },
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
        "entity_accuracy": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "string"}},
                "checked_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_type": {
                                "type": "string",
                                "enum": [
                                    "person", "company", "product",
                                    "institution", "title",
                                    "technical_term", "other",
                                ],
                            },
                            "observed": {"type": "string"},
                            "canonical": {"type": "string"},
                            "verdict": {
                                "type": "string",
                                "enum": [
                                    "correct", "corrected", "incorrect",
                                    "uncertain",
                                ],
                            },
                            "file": {"type": "string"},
                            "evidence_segment_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "source_urls": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notes": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["passed", "issues", "checked_entities"],
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
                    "parent_claim_id": {"type": "string"},
                    "subclaim_id": {"type": "string"},
                    "claim_type": {
                        "type": "string",
                        "enum": list(LEGACY_CLAIM_TYPES),
                    },
                    "claim_origin": {
                        "type": "string",
                        "enum": list(CLAIM_ORIGINS),
                    },
                    "speaker_role": {
                        "type": "string",
                        "enum": list(SPEAKER_ROLES),
                    },
                    "assertion_type": {
                        "type": "string",
                        "enum": list(ASSERTION_TYPES),
                    },
                    "verification_mode": {
                        "type": "string",
                        "enum": list(VERIFICATION_MODES),
                    },
                    "risk_domain": {
                        "type": "string",
                        "enum": list(RISK_DOMAINS),
                    },
                    "verdict": {
                        "type": "string",
                        "enum": list(VERDICTS),
                    },
                    "publication_status": {
                        "type": "string",
                        "enum": list(PUBLICATION_STATUSES),
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
                    "claim", "parent_claim_id", "subclaim_id", "claim_type",
                    "claim_origin", "speaker_role", "assertion_type",
                    "verification_mode", "risk_domain", "verdict",
                    "publication_status",
                    "evidence_segment_ids",
                    "source_urls", "checked_at", "notes",
                ],
            },
        },
    },
    "required": [
        "schema_version", "passed", "summary", "transcript_quality",
        "coverage", "factuality", "numbers", "attribution",
        "entity_accuracy", "tts", "publish", "issues", "fact_checks",
    ],
}


def reviewed_hashes(folder):
    folder = Path(folder)
    return {
        name: sha256(folder / name)
        for name in (*REVIEW_FILES, *OPTIONAL_REVIEW_FILES)
        if (folder / name).exists()
    }


def review_context_hashes(folder):
    """Hash non-authoritative context that may still influence the reviewer."""
    folder = Path(folder)
    cache = folder / CACHE_FILENAME
    return {CACHE_FILENAME: sha256(cache)} if cache.exists() else {}


def review_scope(folder):
    """Describe changed review inputs without trusting the previous verdict."""
    folder = Path(folder)
    current = reviewed_hashes(folder)
    review_path = folder / "ai_review.json"
    try:
        previous = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    expected = previous.get("reviewed_files", {})
    if not isinstance(expected, dict) or not expected:
        return {
            "mode": "full",
            "changed_files": sorted(current),
            "unchanged_files": [],
        }
    changed = sorted(
        name for name, digest in current.items()
        if expected.get(name) != digest
    )
    changed.extend(sorted(name for name in expected if name not in current))
    changed = sorted(set(changed))
    return {
        "mode": "partial_then_full" if changed else "full_confirmation",
        "changed_files": changed,
        "unchanged_files": sorted(set(current) - set(changed)),
    }


def changed_review_inputs(before, after):
    """Return review inputs changed between two hash snapshots."""
    return sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )


def assert_review_snapshot(folder, input_snapshot, context_snapshot=None):
    changed = changed_review_inputs(input_snapshot, reviewed_hashes(folder))
    context_changed = changed_review_inputs(
        context_snapshot or {}, review_context_hashes(folder))
    if changed or context_changed:
        affected = changed + [f"context:{name}" for name in context_changed]
        raise RuntimeError(
            "AI 审查期间输入发生变化，已丢弃结果: "
            + ", ".join(affected))


@contextmanager
def isolated_review_workspace(folder, snapshot, context_snapshot=None):
    """Expose current inputs without a prior verdict and bind all context hashes."""
    folder = Path(folder)
    context_snapshot = dict(context_snapshot or {})
    assert_review_snapshot(folder, snapshot, context_snapshot)
    with tempfile.TemporaryDirectory(prefix="podcast-ai-review-") as td:
        workspace = Path(td)
        for name in snapshot:
            source = folder / name
            destination = workspace / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for name in context_snapshot:
            source = folder / name
            destination = workspace / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        staged = reviewed_hashes(workspace)
        staged_context = review_context_hashes(workspace)
        changed = changed_review_inputs(snapshot, staged)
        context_changed = changed_review_inputs(context_snapshot, staged_context)
        if changed or context_changed:
            affected = changed + [
                f"context:{name}" for name in context_changed]
            raise RuntimeError(
                "AI 审查 staging 哈希不一致: " + ", ".join(affected))
        yield workspace


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



def _current_claim_texts(folder):
    try:
        payload = json.loads(
            (Path(folder) / "content_map.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    claims = []
    for unit in payload.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        for claim in unit.get("claims", []) or []:
            if isinstance(claim, dict):
                text = claim.get("text") or claim.get("claim")
            else:
                text = claim
            text = str(text or "").strip()
            if text:
                claims.append(text)
    return claims


def _write_cache_review_context(folder):
    folder = Path(folder)
    cache_path = folder / CACHE_FILENAME
    if not cache_path.exists():
        return None
    context = build_cache_context(folder, _current_claim_texts(folder))
    path = folder / "fact_check_cache_context.json"
    atomic_write_json(path, context)
    return path

def _prompt(folder, scope=None):
    folder = Path(folder).resolve()
    scope = scope or review_scope(folder)
    scope_instruction = f"""

本次复审范围线索：{scope['mode']}。
- 与上次输入哈希相比发生变化的文件：{scope['changed_files']}。
- 哈希未变化的文件：{scope['unchanged_files']}。
- staging 中不会提供上次 ai_review.json、passed、分数或结论；必须从当前证据独立判断。
- 先重点重审发生变化的文件及其关联 claim；最后必须重新执行一次完整发布判定。
- 即使没有文件变化，也必须独立输出本次审查结果。
"""
    cache_instruction = ""
    if (folder / CACHE_FILENAME).exists():
        cache_instruction = f"""

目录中存在 fact_check_cache_context.json。它是从 {CACHE_FILENAME} 读取并按当前 claim 精确匹配得到的非权威线索：
- 只能使用 matched_entries；expired_dynamic_entries 已被排除；
- 必须确认当前 fact_check 的 claim 和 source URL 都与缓存条目一致；
- 缓存 verdict 不能直接决定本次 verdict，仍需独立检查当前表述和发布日期；
- 最终采用的网页证据仍必须写入本次 fact_checks.source_urls。
"""
    return f"""你是播客流水线的最终发布审查员。请以 max effort 审查目录：
{folder}
{scope_instruction}{cache_instruction}

必须读取：episode.json、来源.md、transcript.raw.json、原始转录.txt、content_map.json、中文完整笔记.md、讲书稿.md、summary_map.json；如果存在，还必须读取 转录_纠错.txt、tts_lexicon.json、editorial_corrections.json、canonical_entities.json 和 source_relevance_cache.json。editorial_corrections.json 只记录外部校正，不得把其中事实伪装成 transcript evidence；canonical_entities.json 是公开名称真源；source_relevance_cache.json 的网页标题和摘录必须与对应 claim/correction 语义相关，URL 可访问不等于来源支持主张。

审查目标：无需人工复核也能直接发布。请执行：
1. 抽查所有章节和对应时间片，检查脑补、漏点、人物归属和逻辑链。
2. 审查 content_map 中每个 claims/numbers/examples 是否被完整笔记或讲稿正确覆盖。
   content_map v3 的 evidence.segment_ids、claim_evidence、claim_evidence_sha256 和 source_sha256 是证据锚点；逐条 claim 必须引用对应的 Sxxxx 片段，不能只相信 summary_map 自报。
3. fact_checks 必须是原子子主张。若一个 content_map claim 同时包含公开事实、说话人观点、第三方指控或编辑推论，必须拆成多条 fact_check：
   - parent_claim_id 必须引用 content_map 中真实存在的 Uxxxx-Cxx。
   - subclaim_id 使用 {{parent_claim_id}}-F01、F02……，在本次审查中唯一。
   - 每条 fact_check 只允许一个主要 assertion_type，不得把事实和观点合并后只给一个分类。
4. 对每个原子子主张分别填写正交维度，禁止把来源和陈述性质混成一个枚举：
   - claim_origin：speaker_firsthand、speaker_reported、external_source、editorial_added、episode_metadata。
   - speaker_role：guest、host、quoted_third_party、editorial、not_applicable、unknown。
   - assertion_type：fact、opinion、prediction、recommendation、explanation、definition、anecdote、allegation、inference。
   - risk_domain：general、medical、legal、financial、political、safety。
   - verification_mode：web_required、source_document_required、web_spot_check、transcript_attribution、transcript_only、safety_cross_check、not_applicable。
5. claim_type 仅作 v2 兼容派生字段：
   - external_source + fact => public_fact。
   - speaker_firsthand + guest + fact/anecdote => guest_firsthand。
   - guest + opinion/prediction/recommendation => guest_opinion。
   - editorial_added + fact => editorial_fact；editorial_added + inference => editorial_inference。
   - 主持人观点、专家解释、定义、第三方指控、节目元数据等没有准确旧分类时必须填 not_applicable，不能硬塞进 guest/public。
6. 按陈述性质选择证据标准：
   - speaker_firsthand：内部数据、亲历事件、未公开研究；不强制联网，核对原话、数字、时间、范围和归因，通常用 transcript_attribution + faithfully_attributed。
   - opinion/prediction：不做真假核查，只检查忠实归因和是否被升级为事实。
   - recommendation：普通建议检查归因；medical/legal/financial/safety 建议使用 safety_cross_check 或明确限制，不能仅靠归因豁免。
   - explanation/definition：核对是否准确表达说话人的解释；一般可 transcript_attribution 或 web_spot_check，高风险领域提高核查强度。
   - allegation 或第三方报道：核查来源文件是否确实这样声称，使用 source_document_required + accurately_reported；不得把“诉状指控”误判为“指控内容已被证明”。
   - external_source/editorial_added 的客观 fact：作为事实采用时通常需要 web_required，优先官方或一手公开来源。
   - episode_metadata：核对 episode.json、来源和转录，通常使用 transcript_only。
7. 将实体准确性作为独立硬门：重点核对公司、人名、产品、机构、职务、技术术语、收购双方和人物归属。下游笔记或讲稿出现名称拼错、主体错配或 ASR 错词未纠正时，entity_accuracy.passed=false，并创建 entity_accuracy high issue。
8. 对公开可验证的金额、百分比、倍数、年份、功率/能量单位、公司规模等高风险数字进行联网交叉验证。说话人内部数字只检查转录忠实性、归因、范围和时间，不要求公开网络存在第二来源。
9. 检查第三方转录中的 ASR 错词、专有名词和明显异常数字。
   如果存在 转录_纠错.txt，它是供下游使用的规范化转录：transcript_quality 应同时评价原始稿的可追溯性和纠错稿是否已消除会影响总结的错误，不要因为保留了不可改写的原始证据而重复扣分。
   对 local_asr 或 legacy_asr，transcript_quality 另外填写 raw_score、
   corrected_score 和 accuracy_basis。没有人工标准逐字稿时不得暗示计算过 WER，
   accuracy_basis 应为 sample_audit 或 semantic_review_only；score 继续表示下游实际
   使用的纠错后综合质量。
   转录纠错只能修复听写、断句、专名和说话人识别错误。节目嘉宾本身说错的事实必须
   保留原话，并在 fact_checks 或稿件归因中纠正，不能把原始发言改写成编辑部事实。
10. 检查中文完整笔记是否确实比精编讲稿更完整；若更短或漏掉重要细节，不能通过。
11. 检查中文完整笔记和公开讲稿是否把“事实状态限定”与“审查决策过程”分开：
   - 应保留“节目称”“报道称”“仍在洽谈”“这是预测而非已发生结果”等影响理解的限定；
   - 禁止出现“这里不采用”“这里不保留”“本稿未独立核实”“由于口径不同因此删除”等后台审查叙述；
   - 若精确数字被舍弃，讲稿应直接自然概括并保留来源归因，不得向听众解释流水线为何删数。
   发现审查过程语言时，publish.passed=false，并创建 briefing_style high issue。
12. 检查讲稿是否适合中文 TTS：阿拉伯数字、英文缩写、专有名词、难读符号和可能误读的混合表达。
   如果存在 tts_lexicon.json，还要检查替换是否会改变原意、误替换子串或引入错误读音。
13. 检查 summary_map 是否真实反映讲稿，而不是只自报 unit IDs。
   同时检查 notes_claim_ids、notes_number_ids、notes_example_ids 与中文完整笔记正文
   是否真实对应；detail item ID 必须逐项绑定 content_map 的 Nxx/Exx。
   哈希规范化规则与 scripts/content_map.py 一致：讲稿按换行后紧跟“## ”切章，
   body_sha256 只计算标题行之后的章节正文，正文先 strip 再做 UTF-8 SHA-256；
   notes_sha256 对中文完整笔记全文先 strip 再做 UTF-8 SHA-256。请按此规则复核，
   不要使用原始文件字节、标题行或其他自行推测的切章方式。
14. publish 只评价上述必读内容文件本身是否已达到发布标准。
   不要读取、检查或评价 HTML、MP3、tts_manifest.json、quality_report.json、publish_report.json、
   文件修改时间、R2、Cloudflare Pages 或线上页面；这些机械产物的新鲜度和可用性由流水线的
   确定性预检负责。不得因为这些产物尚未生成、较旧或未上传而令 publish.passed=false，
   也不得为此创建 issue。只要必读内容文件达到发布标准，publish.passed 必须为 true。

证据输出要求：
- 每个 issue 都必须填写 evidence_type、evidence_segment_ids、source_urls 和 checked_at。
- 联网事实核查必须把最终采用的网页 URL 写入 source_urls；只引用转录时 source_urls 可为空。
- 每条 fact_checks 必须填写 parent_claim_id、唯一 subclaim_id、claim_origin、speaker_role、assertion_type、verification_mode、risk_domain 和派生兼容 claim_type。
- speaker_firsthand、opinion、prediction、recommendation、explanation、definition 和 anecdote 的 source_urls 可以为空，但被采用时必须有 evidence_segment_ids，且 publication_status 与归因一致。
- allegation/source_document_required 必须给出实际诉状、论文、报告或报道 URL；verdict=accurately_reported 只表示来源被准确转述，不表示指控本身为真。
- 对讲稿中实际采用的公开高风险数字、动态事实、重要一手披露、建议、解释、第三方指控和容易混淆的实体相关子主张写入 fact_checks。
- fact_checks.publication_status 必须区分：作为事实采用 used_as_fact、仅按节目观点归因或加限定 attributed_or_qualified、未进入发布稿 excluded。

判定规则：
- 任何 critical/high 问题存在时 passed=false。
- factuality、numbers、attribution、transcript_quality 任一不通过时 passed=false。
- entity_accuracy 不通过时 passed=false；公司名、人名、产品名或主体归属错误通常是 high 问题。
- transcript_quality、coverage、factuality 使用百分制；任何一项低于九十分时 passed=false。
- external_source/editorial_added 的客观 fact 作为事实采用时需要可靠网页依据；speaker_firsthand 只要有转录证据、准确数字、明确归因和范围限定，就可判 faithfully_attributed，不能因无公开网页而扣 factuality 分。
- opinion/prediction 不做真假判定，但如果被写成客观事实则 attribution/factuality 必须失败。
- recommendation/explanation/definition 根据 risk_domain 选择归因、抽查或安全交叉核查；高风险建议不能以“只是嘉宾观点”为由放行。
- allegation 只验证来源是否准确报告；把未裁判指控写成既定事实必须失败。
- 对动态数字注明“节目播出时/节目称”，不能把不断变化的数值写成永久事实。

返回前必须逐条执行以下机械一致性检查；这些规则优先于自然语言直觉：
- speaker_firsthand：普通事实和轶事使用 transcript_attribution；opinion/prediction/recommendation/explanation/definition/allegation 以各自下列专用规则为准，并始终保留 evidence_segment_ids；非 excluded 时 publication_status 不能是 used_as_fact。
- speaker_reported + fact：必须保留明确归因；非 excluded 时 verdict 只能是 accurately_reported、qualified 或 uncertain，publication_status 不能是 used_as_fact。
- opinion/prediction：verification_mode 只能是 transcript_attribution、transcript_only 或 not_applicable，且不能 used_as_fact。
- recommendation：先检查 risk_domain；只要是 medical、legal、financial、safety 中任一值，verification_mode 必须是 safety_cross_check 且 source_urls 必须非空；普通 general/political 建议才使用 transcript_attribution。产品责任、诉讼、合规审批等治理建议属于 legal，不得写成 general 规避安全核查。
- explanation/definition：verification_mode 只能是 transcript_attribution、transcript_only、web_spot_check 或 safety_cross_check；若 verdict 是 faithfully_attributed/accurately_reported/not_applicable，publication_status 必须是 attributed_or_qualified 或 excluded。
- allegation：verification_mode 必须是 source_document_required，必须提供来源 URL，不能 used_as_fact。
- external_source/editorial_added + fact + used_as_fact：verification_mode 必须是 web_required，verdict 必须是 supported 或 qualified。
- 任何 verdict=unsupported/contradicted/faithfully_attributed/accurately_reported/not_applicable 的条目都不得 used_as_fact。
- 最后模拟 quality_report._ai_fact_check_consistency_v3 的上述规则；若任一条不满足，必须先修正 fact_check 再返回。review.passed=true 时 fact_checks 也必须机械一致，不能只让分项文字通过。
- 不要修改任何文件，只返回符合 schema 的 JSON。
"""


def _content_map_claim_ids(workspace):
    try:
        payload = json.loads(
            (Path(workspace) / "content_map.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        f"{unit.get('id')}-C{index:02d}"
        for unit in payload.get("units", [])
        if isinstance(unit, dict) and unit.get("id")
        for index, _claim in enumerate(unit.get("claims", []), start=1)
    }


def _review_semantic_fingerprint(review):
    semantic = {
        key: review.get(key)
        for key in (
            "passed", "summary", "transcript_quality", "coverage",
            "factuality", "numbers", "attribution", "entity_accuracy",
            "tts", "publish", "issues",
        )
    }
    fact_checks = []
    for item in review.get("fact_checks", []) or []:
        if not isinstance(item, dict):
            fact_checks.append(item)
            continue
        fact_checks.append({
            key: item.get(key)
            for key in (
                "claim", "parent_claim_id", "claim_origin", "speaker_role",
                "assertion_type", "risk_domain", "evidence_segment_ids",
            )
        })
    semantic["fact_checks"] = fact_checks
    return semantic


def _mechanical_retry_prompt(review, errors):
    return f"""上一次 AI review 的语义结论已经冻结，但 fact_checks 违反确定性合同。
只允许修正 fact_checks 的机械字段，例如 claim_type、subclaim_id、verification_mode、
verdict、publication_status、source_urls、checked_at 和 notes。不得改变 passed、summary、
任何分项分数或 passed、issues、claim 文本、parent_claim_id、claim_origin、speaker_role、
assertion_type、risk_domain 或 evidence_segment_ids。需要 URL 时必须联网找到真实来源，
不得编造。返回完整 REVIEW_SCHEMA JSON，不修改文件。

确定性错误：
{json.dumps(errors, ensure_ascii=False)}

冻结的首次输出：
{json.dumps(review, ensure_ascii=False)}
"""


def _validate_or_retry_review(workspace, review, *, model, effort):
    normalize_review_fact_checks(review)
    claim_ids = _content_map_claim_ids(workspace)
    errors, warnings = validate_review_fact_checks(
        review, valid_claim_ids=claim_ids)
    audit = {
        "initial_errors": errors,
        "initial_warnings": warnings,
        "retry_count": 0,
    }
    if not errors:
        return review, audit, None
    semantic = _review_semantic_fingerprint(review)
    retry_result = run_json_task(
        workspace,
        _mechanical_retry_prompt(review, errors) + (
            f"\n本次机械纠错 effort 要求：{effort}。只返回 JSON。"),
        REVIEW_SCHEMA,
        task_name="ai_review_mechanical_retry",
        enable_search=True,
        model=model or None,
        timeout=900,
    )
    corrected = retry_result.get("payload")
    if not isinstance(corrected, dict):
        raise RuntimeError("AI review 机械纠错输出必须是对象")
    normalize_review_fact_checks(corrected)
    if _review_semantic_fingerprint(corrected) != semantic:
        raise RuntimeError("AI review 机械纠错修改了冻结的语义结论")
    final_errors, final_warnings = validate_review_fact_checks(
        corrected, valid_claim_ids=claim_ids)
    audit.update({
        "retry_count": 1,
        "final_errors": final_errors,
        "final_warnings": final_warnings,
    })
    if final_errors:
        raise RuntimeError(
            "AI review 机械纠错后仍不符合合同: "
            + "; ".join(final_errors[:10]))
    return corrected, audit, retry_result


def run_ai_review(folder, output=None, model=None, effort="max", *, persist=True):
    folder = Path(folder).resolve()
    missing = [name for name in REVIEW_FILES if not (folder / name).exists()]
    if missing:
        raise RuntimeError(f"缺少 AI 审查输入文件: {missing}")
    print(
        f"[AI审查] subagent model={model or 'default'} "
        f"effort={effort} folder={folder.name}",
        file=sys.stderr, flush=True)
    refresh_source_relevance_cache(folder)
    input_snapshot = reviewed_hashes(folder)
    context_snapshot = review_context_hashes(folder)
    scope = review_scope(folder)
    schema_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False)
    schema_path = Path(schema_file.name)
    try:
        json.dump(REVIEW_SCHEMA, schema_file, ensure_ascii=False)
        schema_file.close()
        with isolated_review_workspace(
                folder, input_snapshot, context_snapshot) as workspace:
            _write_cache_review_context(workspace)
            result = run_json_task(
                workspace,
                _prompt(workspace, scope) + (
                    f"\n本次审查 effort 要求：{effort}。"
                    "只返回符合 schema 的 JSON，不修改任何文件。"),
                schema_path, task_name="ai_review", enable_search=True,
                model=model or None, timeout=1800)
            review = result["payload"]
            review, mechanical_audit, retry_result = _validate_or_retry_review(
                workspace,
                review,
                model=model,
                effort=effort,
            )
            if retry_result is not None:
                result = {
                    **retry_result,
                    "duration_ms": (
                        (result.get("duration_ms") or 0)
                        + (retry_result.get("duration_ms") or 0)
                    ),
                    "retry_count": (
                        (result.get("retry_count") or 0)
                        + (retry_result.get("retry_count") or 0)
                    ),
                }
            review["mechanical_validation"] = mechanical_audit
    finally:
        try:
            schema_file.close()
        finally:
            schema_path.unlink(missing_ok=True)

    assert_review_snapshot(folder, input_snapshot, context_snapshot)

    review["schema_version"] = AI_REVIEW_SCHEMA_VERSION
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
        "mechanical_retry_count": review.get(
            "mechanical_validation", {}).get("retry_count", 0),
    }
    review["review_scope"] = scope
    review["reviewed_files"] = input_snapshot
    review["review_context"] = context_snapshot
    review["input_snapshot_verified"] = True
    if persist:
        output = Path(output) if output else folder / "ai_review.json"
        assert_review_snapshot(folder, input_snapshot, context_snapshot)
        atomic_write_json(output, review)
        review["fact_check_cache_entries_written"] = (
            update_cache_from_review(folder, review))
    return review


def review_status_transition(folder, passed):
    try:
        from episode import (
            apply_review_status_update, build_review_status_update)
    except ImportError:
        from scripts.episode import (
            apply_review_status_update, build_review_status_update)
    payload, source_text = build_review_status_update(folder, passed)
    return payload, source_text, apply_review_status_update


def update_source_status(folder, passed):
    payload, source_text, apply_update = review_status_transition(folder, passed)
    return apply_update(folder, payload, source_text)


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
            review = run_ai_review(
                folder, output, model, effort, persist=False)
            before_status = dict(review["reviewed_files"])
            context_snapshot = dict(review.get("review_context", {}))
            expected_episode, expected_source, apply_update = (
                review_status_transition(
                    folder, review.get("passed", False)))
            # The expected transition is built from the reviewed files. Recheck
            # before applying it so a concurrent edit is never folded into trust.
            assert_review_snapshot(
                folder, before_status, context_snapshot)
            apply_update(
                folder, expected_episode, expected_source)
            actual_episode = json.loads(
                (folder / "episode.json").read_text(encoding="utf-8"))
            actual_source = (folder / "来源.md").read_text(encoding="utf-8")
            if (
                    actual_episode != expected_episode
                    or actual_source != expected_source):
                raise RuntimeError(
                    "AI 审查后状态绑定内容超出预期 metadata transition")
            after_status = reviewed_hashes(folder)
            status_changes = changed_review_inputs(before_status, after_status)
            expected_changes = {"episode.json", "来源.md"}
            if set(status_changes) - expected_changes:
                raise RuntimeError(
                    "AI 审查后状态绑定出现非预期输入变化: "
                    + ", ".join(status_changes))
            review["status_binding"] = {
                "trusted_metadata_files": status_changes,
                "reason": "exact_pipeline_review_status_transition",
            }
            review["reviewed_files"] = after_status
            # Last check is immediately adjacent to the authoritative write.
            assert_review_snapshot(
                folder, after_status, context_snapshot)
            atomic_write_json(output, review)
            try:
                review["fact_check_cache_entries_written"] = (
                    update_cache_from_review(folder, review))
            except Exception as exc:
                review["fact_check_cache_update_error"] = str(exc)
                print(
                    f"[AI审查][警告] fact-check cache 更新失败: {exc}",
                    file=sys.stderr, flush=True)
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

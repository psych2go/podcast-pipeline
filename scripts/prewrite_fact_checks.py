"""Pre-writing fact-check ledger for source-faithful podcast drafting."""
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from content_map import body_sha256
    from hashing import sha256_file
    from subagent import run_json_task
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.content_map import body_sha256
    from scripts.hashing import sha256_file
    from scripts.subagent import run_json_task


FILENAME = "editorial_fact_checks.json"
SCHEMA_VERSION = 1
RISK_LEVELS = ("low", "medium", "high")
RISK_DOMAINS = ("general", "medical", "legal", "financial", "political", "safety")
ASSERTION_TYPES = (
    "fact", "opinion", "prediction", "recommendation", "explanation",
    "definition", "anecdote", "allegation", "inference",
)
CLAIM_ORIGINS = (
    "speaker_firsthand", "speaker_reported", "external_source",
    "editorial_added", "episode_metadata",
)
VERIFICATION_MODES = (
    "web_required", "source_document_required", "web_spot_check",
    "transcript_attribution", "transcript_only", "safety_cross_check",
    "not_applicable",
)
VERDICTS = (
    "supported", "qualified", "contradicted", "uncertain",
    "faithfully_attributed", "accurately_reported", "not_applicable",
)


def _transcript_basis(folder):
    folder = Path(folder)
    corrected = folder / "转录_纠错.txt"
    path = corrected if corrected.exists() else folder / "原始转录.txt"
    return {
        "file": path.name,
        "sha256": body_sha256(path.read_text(encoding="utf-8")),
    }


def claim_inventory(content_map):
    """Return every non-excluded source claim in stable content-map order."""
    inventory = []
    for unit in content_map.get("units", []) or []:
        if not isinstance(unit, dict) or unit.get("status") == "excluded":
            continue
        unit_id = str(unit.get("id") or "").strip()
        segment_ids = list(unit.get("evidence", {}).get("segment_ids", []) or [])
        for index, raw_claim in enumerate(unit.get("claims", []) or [], start=1):
            if isinstance(raw_claim, dict):
                claim = raw_claim.get("text") or raw_claim.get("claim")
            else:
                claim = raw_claim
            claim = str(claim or "").strip()
            if not unit_id or not claim:
                continue
            inventory.append({
                "parent_claim_id": f"{unit_id}-C{index:02d}",
                "source_claim": claim,
                "unit_importance": str(unit.get("importance") or "low"),
                "evidence_segment_ids": segment_ids,
            })
    return inventory


LEDGER_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
        "generated_at": {"type": "string"},
        "content_map_sha256": {"type": "string"},
        "transcript_basis": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "sha256": {"type": "string"},
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parent_claim_id": {"type": "string"},
                    "source_claim": {"type": "string"},
                    "unit_importance": {
                        "type": "string", "enum": ["low", "medium", "high"],
                    },
                    "risk_level": {"type": "string", "enum": list(RISK_LEVELS)},
                    "risk_domains": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(RISK_DOMAINS)},
                    },
                    "requires_web": {"type": "boolean"},
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subclaim_id": {"type": "string"},
                                "statement": {"type": "string"},
                                "claim_origin": {
                                    "type": "string", "enum": list(CLAIM_ORIGINS),
                                },
                                "assertion_type": {
                                    "type": "string", "enum": list(ASSERTION_TYPES),
                                },
                                "verification_mode": {
                                    "type": "string", "enum": list(VERIFICATION_MODES),
                                },
                                "risk_domain": {
                                    "type": "string", "enum": list(RISK_DOMAINS),
                                },
                                "verdict": {
                                    "type": "string", "enum": list(VERDICTS),
                                },
                                "editorial_correction": {"type": "string"},
                                "source_urls": {
                                    "type": "array", "items": {"type": "string"},
                                },
                                "checked_at": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "issue_inventory": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parent_claim_id": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {"type": "string"},
                    "statement": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "evidence_segment_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "source_urls": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
            },
        },
        "summary": {
            "type": "object",
            "properties": {
                "claim_count": {"type": "integer", "minimum": 0},
                "checked_subclaim_count": {"type": "integer", "minimum": 0},
                "issue_count": {"type": "integer", "minimum": 0},
                "exhaustive_inventory_completed": {"type": "boolean"},
            },
        },
    },
}


def validate_ledger(folder, payload=None):
    """Validate freshness and exact source-claim coverage of a ledger."""
    folder = Path(folder)
    path = folder / FILENAME
    if payload is None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"{FILENAME} 无法读取: {exc}"]
    errors = []
    if not isinstance(payload, dict):
        return [f"{FILENAME} 必须是 JSON 对象"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{FILENAME}.schema_version 无效")
    content_map_path = folder / "content_map.json"
    try:
        content_map = json.loads(content_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"content_map.json 无法读取: {exc}"]
    if payload.get("content_map_sha256") != sha256_file(content_map_path):
        errors.append(f"{FILENAME} 绑定的 content_map.json 哈希已过期")
    try:
        basis = _transcript_basis(folder)
    except OSError as exc:
        return errors + [f"{FILENAME} 无法验证转录基准: {exc}"]
    if payload.get("transcript_basis") != basis:
        errors.append(f"{FILENAME} 绑定的转录基准已过期")

    expected = claim_inventory(content_map)
    actual = payload.get("claims")
    if not isinstance(actual, list):
        return errors + [f"{FILENAME}.claims 必须是数组"]
    expected_pairs = [
        (item["parent_claim_id"], item["source_claim"])
        for item in expected
    ]
    actual_pairs = [
        (str(item.get("parent_claim_id", "")), str(item.get("source_claim", "")))
        for item in actual if isinstance(item, dict)
    ]
    if actual_pairs != expected_pairs:
        errors.append(f"{FILENAME} 未按顺序完整覆盖当前 source claims")
    if len(actual) != len(actual_pairs):
        errors.append(f"{FILENAME}.claims 含非对象条目")

    known_ids = {item["parent_claim_id"] for item in expected}
    for record in actual:
        if not isinstance(record, dict):
            continue
        parent = str(record.get("parent_claim_id", ""))
        checks = record.get("checks")
        if not isinstance(checks, list):
            errors.append(f"{parent or 'unknown'}: checks 必须是数组")
            continue
        if not checks:
            errors.append(f"{parent or 'unknown'}: 至少需要一个原子 fact check")
        risk_domains = record.get("risk_domains")
        if not isinstance(risk_domains, list) or not risk_domains:
            errors.append(f"{parent or 'unknown'}: risk_domains 不能为空")
        seen = set()
        has_web_source = False
        for check in checks:
            if not isinstance(check, dict):
                errors.append(f"{parent}: checks 含非对象条目")
                continue
            subclaim_id = str(check.get("subclaim_id", ""))
            if not subclaim_id.startswith(f"{parent}-F") or subclaim_id in seen:
                errors.append(f"{parent}: subclaim_id 无效或重复: {subclaim_id!r}")
            seen.add(subclaim_id)
            correction = str(check.get("editorial_correction", "")).strip()
            urls = check.get("source_urls") or []
            has_web_source = has_web_source or bool(urls)
            if correction and not urls:
                errors.append(f"{subclaim_id}: 编辑纠正缺少来源 URL")
        if record.get("requires_web") is True and not has_web_source:
            errors.append(f"{parent}: 标记 requires_web 但没有网页来源")
    for issue in payload.get("issue_inventory", []) or []:
        if not isinstance(issue, dict):
            errors.append(f"{FILENAME}.issue_inventory 含非对象条目")
            continue
        if issue.get("parent_claim_id") not in known_ids:
            errors.append(
                f"{FILENAME} issue 引用未知 claim: {issue.get('parent_claim_id')!r}")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append(f"{FILENAME}.summary 必须是对象")
    else:
        check_count = sum(
            len(item.get("checks", []) or [])
            for item in actual if isinstance(item, dict)
        )
        if summary.get("claim_count") != len(expected):
            errors.append(f"{FILENAME}.summary.claim_count 不匹配")
        if summary.get("checked_subclaim_count") != check_count:
            errors.append(f"{FILENAME}.summary.checked_subclaim_count 不匹配")
        if summary.get("issue_count") != len(payload.get("issue_inventory", []) or []):
            errors.append(f"{FILENAME}.summary.issue_count 不匹配")
        if summary.get("exhaustive_inventory_completed") is not True:
            errors.append(f"{FILENAME} 未声明完成穷尽式问题清单")
    return errors


def ledger_is_current(folder):
    path = Path(folder) / FILENAME
    if not path.exists():
        return False
    return not validate_ledger(folder)


def _prompt(folder, inventory, content_map_sha256, transcript_basis):
    return f"""你是播客流水线的预写作事实核查员。目录：{Path(folder).resolve()}

只读文件：transcript.raw.json、原始转录.txt、content_map.json、来源.md，以及存在时的转录_纠错.txt。
不得修改任何文件。不得把外部纠正写回 content_map；content_map 永远只表示节目实际说了什么。

必须对下面 inventory 中每一条 source claim 按原顺序返回一条 claims 记录，不得遗漏、合并、重排或改写 source_claim：
{json.dumps(inventory, ensure_ascii=False)}

绑定值必须原样返回：
- schema_version={SCHEMA_VERSION}
- content_map_sha256={content_map_sha256}
- transcript_basis={json.dumps(transcript_basis, ensure_ascii=False)}

执行要求：
1. 把每条 source claim 拆成原子 checks。人名、书名、公司、机构、药物、医学解剖、金额、比例、年份、倍数和动态事实必须单独成 check。
2. 对 medical/legal/financial/political/safety 的客观事实，以及公开可验证的重要实体和精确数字，使用网页搜索并优先官方、一手论文或原始报告。
3. 说话人观点、预测、解释和一手经历只检查转录忠实性与归因，不因缺少网页而判错。
4. 每个 check 的 subclaim_id 必须严格使用 {{parent_claim_id}}-F01、F02……连续编号；外部来源与节目原话冲突时，保留 source_claim 不变，把自然、可直接供中文作者采用的纠正写入 editorial_correction，并提供实际 source_urls。
5. issue_inventory 必须一次性穷尽列出所有 critical/high/medium/low 问题；不得发现第一个高风险问题后停止。
6. 每个 critical/high/medium issue 必须有明确 recommendation；联网问题必须有 source_urls。
7. summary 数字必须与实际数组一致，exhaustive_inventory_completed 必须为 true。
8. 返回符合 schema 的 JSON，不要输出解释文字。
"""


def _sanitize_unsourced_corrections(payload):
    """Drop model-authored corrections that have no auditable web source."""
    for record in payload.get("claims", []) or []:
        if not isinstance(record, dict):
            continue
        for check in record.get("checks", []) or []:
            if not isinstance(check, dict):
                continue
            correction = str(check.get("editorial_correction", "")).strip()
            urls = check.get("source_urls") or []
            if not correction or urls:
                continue
            check["editorial_correction"] = ""
            check["verdict"] = "uncertain"
            if check.get("claim_origin") in {
                    "speaker_firsthand", "speaker_reported", "episode_metadata"}:
                check["verification_mode"] = "transcript_attribution"
            note = str(check.get("notes", "")).strip()
            suffix = "无可审计来源的编辑纠正已由流水线丢弃；只保留节目归因。"
            check["notes"] = f"{note} {suffix}".strip()
    return payload


def _normalize_subclaim_ids(payload):
    """Assign pipeline-owned Fxx IDs while preserving model check order."""
    for record in payload.get("claims", []) or []:
        if not isinstance(record, dict):
            continue
        parent = str(record.get("parent_claim_id", ""))
        for index, check in enumerate(record.get("checks", []) or [], start=1):
            if isinstance(check, dict):
                check["subclaim_id"] = f"{parent}-F{index:02d}"
    return payload


def run_prewrite_fact_checks(folder, *, model=None, effort="high"):
    """Generate and persist a complete pre-writing fact-check ledger."""
    folder = Path(folder).resolve()
    content_map_path = folder / "content_map.json"
    content_map = json.loads(content_map_path.read_text(encoding="utf-8"))
    inventory = claim_inventory(content_map)
    map_hash = sha256_file(content_map_path)
    basis = _transcript_basis(folder)
    result = run_json_task(
        folder,
        _prompt(folder, inventory, map_hash, basis)
        + f"\n本次核查 effort={effort}。",
        LEDGER_SCHEMA,
        task_name="prewrite_fact_checks",
        enable_search=True,
        model=model or None,
        timeout=1800,
    )
    payload = result["payload"]
    # Binding fields are pipeline-owned; the research model cannot choose
    # which semantic revision its findings authorize.
    payload["schema_version"] = SCHEMA_VERSION
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["content_map_sha256"] = map_hash
    payload["transcript_basis"] = basis
    # Subclaim IDs are mechanical bindings owned by the pipeline, not a
    # research-model choice. Preserve returned order and normalize IDs.
    _sanitize_unsourced_corrections(payload)
    _normalize_subclaim_ids(payload)
    errors = validate_ledger(folder, payload)
    if errors:
        raise RuntimeError("预写作事实台账无效: " + "; ".join(errors[:10]))
    atomic_write_json(folder / FILENAME, payload)
    return {
        "claim_count": len(payload.get("claims", [])),
        "checked_subclaim_count": payload.get("summary", {}).get(
            "checked_subclaim_count", 0),
        "issue_count": len(payload.get("issue_inventory", [])),
        "duration_ms": result.get("duration_ms"),
    }

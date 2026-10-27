"""Content repair for selected review contract failures, never verdict repair.

All provider work and finalization happen in staging. The original failed review
is diagnostic only; publication always requires another independent review.
"""
import copy
import json
import re
import tempfile
from datetime import datetime, UTC
from pathlib import Path

from scripts.atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from scripts.content_map import (
    coverage_report, load_json, validate_content_map, validate_summary_map,
)
from scripts.editorial_corrections import validate_editorial_corrections
from scripts.source_relevance import _fetch_source
from scripts.subagent import run_json_task

PROSE_FILES = ("中文完整笔记.md", "讲书稿.md")
REPAIR_FILES = (
    *PROSE_FILES, "summary_map.json", "content_map.json", "tts_lexicon.json",
    "editorial_fact_checks.json", "editorial_corrections.json",
)
SPECIALIZED = {"opinion", "prediction", "recommendation", "explanation", "definition", "allegation"}


def contract_targets(diagnostic):
    """Select by taxonomy, not exception wording; unrelated errors stay closed."""
    review = diagnostic.get("original_output") or {}
    targets = []
    for item in review.get("fact_checks", []) or []:
        if not isinstance(item, dict) or item.get("publication_status") == "excluded":
            continue
        assertion = item.get("assertion_type")
        kind = None
        if (assertion == "recommendation"
                and item.get("risk_domain") in {"medical", "legal", "financial", "safety"}
                and (item.get("verification_mode") != "safety_cross_check"
                     or not item.get("source_urls"))):
            kind = "safety_cross_check"
        elif assertion == "allegation" and (
                not item.get("source_urls") or item.get("publication_status") == "used_as_fact"
                or item.get("verdict") not in {
                    "accurately_reported", "qualified", "unsupported",
                    "contradicted", "uncertain"}):
            kind = "source_document"
        elif (item.get("claim_origin") == "speaker_firsthand"
              and assertion not in SPECIALIZED
              and (item.get("verdict") not in {"faithfully_attributed", "qualified"}
                   or item.get("verification_mode") != "transcript_attribution"
                   or item.get("publication_status") == "used_as_fact")):
            kind = "firsthand_attribution"
        elif (item.get("claim_origin") == "speaker_reported" and assertion == "fact"
              and (item.get("verdict") not in {
                       "accurately_reported", "qualified", "uncertain"}
                   or item.get("publication_status") == "used_as_fact")):
            kind = "firsthand_attribution"
        elif (assertion in {"opinion", "prediction", "inference"}
              and item.get("publication_status") == "used_as_fact"):
            kind = "firsthand_attribution"
        elif (item.get("claim_origin") in {"external_source", "editorial_added"}
              and assertion == "fact" and item.get("publication_status") == "used_as_fact"
              and (not item.get("source_urls")
                   or item.get("verdict") not in {"supported", "qualified"})):
            kind = "public_source"
        if kind:
            targets.append({**copy.deepcopy(item), "repair_reason": kind})
    return targets


def failed_review_targets(review):
    """Source/attribution findings in an otherwise well-formed failed review."""
    targets = contract_targets({"original_output": review})
    issues = [i for i in review.get("issues", []) or [] if isinstance(i, dict)
              and i.get("severity") in {"critical", "high"}]
    semantic = [i for i in issues if re.search(
        r"factual|attribut|safety|allegation", str(i.get("category", "")), re.I)]
    if not semantic:
        return targets
    ids = set(re.findall(r"U\d+-C\d+", json.dumps(semantic, ensure_ascii=False)))
    known = {t.get("subclaim_id") for t in targets}
    for item in review.get("fact_checks", []) or []:
        if (not isinstance(item, dict) or item.get("publication_status") == "excluded"
                or item.get("subclaim_id") in known
                or (ids and item.get("parent_claim_id") not in ids)):
            continue
        assertion = item.get("assertion_type")
        if assertion == "recommendation" and item.get("risk_domain") != "general":
            reason = "safety_cross_check"
        elif assertion == "allegation":
            reason = "source_document"
        elif item.get("claim_origin") in {"speaker_firsthand", "speaker_reported"}:
            reason = "firsthand_attribution"
        elif assertion == "fact":
            reason = "public_source"
        else:
            continue
        targets.append({**copy.deepcopy(item), "repair_reason": reason})
    return targets


SOURCE_SCHEMA = {
    "type": "object", "properties": {"sources": {
        "type": "array", "maxItems": 12, "items": {
            "type": "object", "properties": {
                "parent_claim_id": {"type": "string"},
                "purpose": {"type": "string", "enum": [
                    "safety_cross_check", "source_document", "firsthand_attribution",
                    "public_source"]},
                "url": {"type": "string"}, "quote": {"type": "string"},
                "explanation": {"type": "string"},
            },
        },
    }},
}
PLAN_SCHEMA = {
    "type": "object", "properties": {
        "replacements": {"type": "array", "items": {
            "type": "object", "properties": {
                "file": {"type": "string", "enum": list(PROSE_FILES)},
                "old": {"type": "string"}, "new": {"type": "string"},
            },
        }},
        "remove_claim_ids": {"type": "array", "items": {"type": "string"}},
        "remove_number_ids": {"type": "array", "items": {"type": "string"}},
        "remove_example_ids": {"type": "array", "items": {"type": "string"}},
    },
}


def lookup_sources(workspace, targets, *, model="", effort="max"):
    prompt = f"""为以下审查阻断项执行一次有界网页检索，不修改文件、不返回审查 verdict。
必须实际搜索并打开公开页面；最多返回十二个直接来源和可逐字核对的 quote，找不到返回空 sources。
safety_cross_check 必须找可靠机构的医疗/法律/金融/代码安全指导，解释适用范围，不能把通用指导
当作节目中精确数字的证明；source_document 必须是原始文件、法院/监管/公司公告或直接报道，
只证明指控被提出，不证明指控成立。一手信息优先公开原始声明，不能借外部数字改写嘉宾原话。
public_source 必须为直接支持该客观事实的公开材料；找不到时不要改 verdict，后续软化或删除。
拒绝猜测 URL、搜索结果页、首页和不相关材料。purpose 必须对应 repair_reason。
{json.dumps(targets, ensure_ascii=False)}"""
    result = run_json_task(
        workspace, prompt, SOURCE_SCHEMA, task_name="review_contract_sources",
        enable_search=True, model=model or None, timeout=600, effort=effort,
    )
    allowed = {(item["parent_claim_id"], item["repair_reason"]) for item in targets}
    accepted = []
    rejected = []
    for source in result["payload"].get("sources", [])[:12]:
        try:
            if (source["parent_claim_id"], source["purpose"]) not in allowed:
                raise ValueError("来源不属于本轮目标")
            # The shared fetcher rejects private hosts, checks TLS, pins public
            # addresses and bounds redirects/bytes. A model-supplied URL alone
            # is never evidence. Require a quote in the fetched public text.
            fetched = _fetch_source(source["url"])
            quote = re.sub(r"\s+", " ", source["quote"]).strip()
            text = re.sub(r"\s+", " ", fetched.get("excerpt", "")).strip()
            if len(quote) < 20 or quote.casefold() not in text.casefold():
                raise ValueError("公开页面摘录不能复现检索引文")
            accepted.append({**source, "content_sha256": fetched["content_sha256"]})
        except Exception as exc:
            rejected.append({"source": source, "error": str(exc)})
    return accepted, rejected


def _remap(value, mapping):
    """Remap exact IDs (including atomic subclaims); None drops references."""
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        match = re.fullmatch(r"(U\d+-C\d+)(-F\d+)", value)
        if match and match[1] in mapping:
            parent = mapping[match[1]]
            return parent + match[2] if parent else None
        return value
    if isinstance(value, list):
        result = [_remap(item, mapping) for item in value]
        return [item for item in result if item is not None]
    if isinstance(value, dict):
        if any(value.get(key) in mapping and mapping[value[key]] is None
               for key in ("parent_claim_id", "claim_id") if isinstance(value.get(key), str)):
            return None
        return {key: _remap(item, mapping) for key, item in value.items()}
    return value


def remove_claims(workspace, claim_ids, number_ids=(), example_ids=()):
    """Remove selected generated claims, reindex dependencies, retain raw evidence.

    Source segments and unit evidence remain for source accountability; only
    per-claim evidence for deleted claims is removed. No source text is edited.
    """
    content = load_json(workspace / "content_map.json")
    known = {f"{u['id']}-C{i:02d}" for u in content["units"]
             for i, _ in enumerate(u.get("claims", []), 1)}
    if not set(claim_ids) <= known:
        raise ValueError("删除包含未知 claim ID")
    mapping = {}
    removed = []
    for unit in content["units"]:
        uid = unit["id"]
        old_claims = unit.get("claims", [])
        keep = [i for i in range(len(old_claims)) if f"{uid}-C{i+1:02d}" not in claim_ids]
        if len(keep) == len(old_claims):
            continue
        for i, claim in enumerate(old_claims):
            old = f"{uid}-C{i+1:02d}"
            mapping[old] = f"{uid}-C{keep.index(i)+1:02d}" if i in keep else None
            if i not in keep:
                removed.append({"claim_id": old, "source_claim": claim})
        unit["claims"] = [old_claims[i] for i in keep]
        if "claim_modalities" in unit:
            unit["claim_modalities"] = [unit["claim_modalities"][i] for i in keep]
        for field in ("claim_evidence", "claim_evidence_roles", "claim_evidence_sha256",
                      "claim_evidence_notes"):
            if field in unit:
                unit[field] = {f"C{j:02d}": unit[field][f"C{i+1:02d}"]
                               for j, i in enumerate(keep, 1)
                               if f"C{i+1:02d}" in unit[field]}
        if not keep:
            unit.update(status="excluded", exclusion_type="other",
                        notes="独立审查合同阻断后检索无可靠支持，删除无法安全限定的公开主张；原始证据保留。")
            mapping[uid] = None
        for field, ids, prefix in (("numbers", number_ids, "N"), ("examples", example_ids, "E")):
            texts = unit.get(field, []) or []
            retained = [i for i in range(len(texts))
                        if keep and f"{uid}-{prefix}{i+1:02d}" not in ids]
            for i in range(len(texts)):
                mapping[f"{uid}-{prefix}{i+1:02d}"] = (
                    f"{uid}-{prefix}{retained.index(i)+1:02d}" if i in retained else None)
            unit[field] = [texts[i] for i in retained]
            item_field = "number_items" if field == "numbers" else "example_items"
            if item_field in unit:
                unit[item_field] = [{"id": f"{prefix}{i:02d}", "text": text}
                                    for i, text in enumerate(unit[field], 1)]
    atomic_write_json(workspace / "content_map.json", content)
    for name in ("summary_map.json", "editorial_fact_checks.json", "editorial_corrections.json"):
        path = workspace / name
        if not path.exists():
            continue
        payload = _remap(load_json(path), mapping)
        if name == "editorial_fact_checks.json":
            records = payload.get("claims", [])
            payload["summary"].update(
                claim_count=len(records),
                checked_subclaim_count=sum(len(r.get("checks", [])) for r in records),
                issue_count=len(payload.get("issue_inventory", [])))
        atomic_write_json(path, payload)
    return removed


def _attach_sources(workspace, sources):
    path = workspace / "editorial_corrections.json"
    payload = load_json(path) if path.exists() else {"schema_version": 1, "corrections": []}
    for source in sources:
        payload["corrections"].append({
            "correction_id": f"contract-source-{len(payload['corrections'])+1}",
            "claim_id": source["parent_claim_id"], "episode_statement": source["claim"],
            "public_treatment": source["explanation"], "verdict": "uncertain",
            "risk_domain": source["risk_domain"], "checked_at": datetime.now(UTC).isoformat(),
            "source_urls": [source["url"]], "source_quote": source["quote"],
            "purpose": source["purpose"], "content_sha256": source["content_sha256"],
        })
    if sources:
        atomic_write_json(path, payload)


def repair_contract_failure(folder, diagnostic, *, model="", effort="max"):
    from scripts.ai_review import OPTIONAL_REVIEW_FILES, REVIEW_FILES
    from scripts.review_repair import _refresh_semantic_bindings

    folder = Path(folder)
    targets = diagnostic.get("repair_targets") or contract_targets(diagnostic)
    if not targets:
        raise ValueError("没有可自动修复的合同阻断项")
    names = set(REVIEW_FILES + OPTIONAL_REVIEW_FILES)
    snapshots = {name: (folder / name).read_bytes() if (folder / name).exists() else None
                 for name in names}
    with tempfile.TemporaryDirectory(prefix="review-contract-repair-") as tmp:
        workspace = Path(tmp)
        for name, data in snapshots.items():
            if data is not None:
                atomic_write_bytes(workspace / name, data)
        try:
            sources, rejected = lookup_sources(workspace, targets, model=model, effort=effort)
        except Exception as exc:
            # A failed lookup is not permission to fabricate a source. Fall
            # back to explicit attribution/softening/removal in the same round.
            sources, rejected = [], [{"error": str(exc)}]
        for source in sources:
            target = next(t for t in targets if t["parent_claim_id"] == source["parent_claim_id"]
                          and t["repair_reason"] == source["purpose"])
            source.update(claim=target["claim"], risk_domain=target["risk_domain"])
        prompt = f"""只返回本轮合同阻断项的最小精确文本修订计划，不改文件，不返回审查 verdict。
读取两份中文稿、content_map、summary_map 和转录，必须同时修订笔记与讲稿中的相关说法。
有来源时自然附上直接 URL，并区分原始节目说法和外部信息，不得把安全指导当作数字证明。
无来源时优先软化并明确真实说话人（如‘据节目中X所说’）；X 必须来自转录，不能猜。
高风险建议仅加‘节目称’仍不够：无安全来源时必须改成非行动性讨论，不保留投资/医疗/
法律/部署操作建议。无原始来源的指控不能只加归因继续传播；改成非指控概括或删除。
一手数据不冒充独立事实，保留必要归因、范围；无法安全概括的精确数字直接删去。
若删除整条 source claim，填写 remove_claim_ids，并删除两稿相关论断、数字、例子；填写
依赖的 remove_number_ids/remove_example_ids（Uxxxx-Nxx/Exx）。不要删除其他 claim。
只返回两稿的唯一精确 old/new 替换；不得改标题或输出后台审查语言。保留未涉问题内容。
绑定、证据、数字和 summary 映射由主流程在删除后同步，不得伪造覆盖。
合同阻断项：{json.dumps(targets, ensure_ascii=False)}
实际取回且引文可复现的来源（仅线索，下一轮独立复核）：{json.dumps(sources, ensure_ascii=False)}
"""
        plan = run_json_task(
            workspace, prompt, PLAN_SCHEMA, task_name="review_contract_revision",
            enable_search=False, model=model or None, timeout=900, effort=effort,
        )["payload"]
        allowed_ids = {t["parent_claim_id"] for t in targets}
        removed_ids = set(plan.get("remove_claim_ids", []))
        if not removed_ids <= allowed_ids:
            raise ValueError("合同修复不得删除非目标 claim")
        removed_units = {cid.split("-", 1)[0] for cid in removed_ids}
        for field in ("remove_number_ids", "remove_example_ids"):
            if any(value.split("-", 1)[0] not in removed_units for value in plan.get(field, [])):
                raise ValueError("合同修复不得删除非目标数字或例子")
        changed = False
        edits = {name: [] for name in PROSE_FILES}
        original_prose = {name: (workspace / name).read_text(encoding="utf-8")
                          for name in PROSE_FILES}
        for replacement in plan.get("replacements", []):
            name, old, new = (replacement[k] for k in ("file", "old", "new"))
            if name not in PROSE_FILES or not old.strip() or old == new:
                raise ValueError("合同修复必须是受限的精确稿件替换")
            text = original_prose[name]
            if text.count(old) != 1:
                raise ValueError("合同修复的 old 必须唯一匹配")
            start = text.index(old)
            end = start + len(old)
            if any(start < stop and end > begin for begin, stop, _ in edits[name]):
                raise ValueError("合同修复不允许重叠替换")
            edits[name].append((start, end, new))
            changed = True
        for name, replacements in edits.items():
            text = original_prose[name]
            for start, end, new in sorted(replacements, reverse=True):
                text = text[:start] + new + text[end:]
            atomic_write_text(workspace / name, text)
        if not changed and not sources:
            raise ValueError("合同修复没有改变稿件或补充真实来源")
        _attach_sources(workspace, sources)
        removed = remove_claims(workspace, removed_ids, plan.get("remove_number_ids", []),
                                plan.get("remove_example_ids", [])) if removed_ids else []
        _refresh_semantic_bindings(workspace, [])
        content = load_json(workspace / "content_map.json")
        transcript = load_json(workspace / "transcript.raw.json")
        summary = load_json(workspace / "summary_map.json")
        notes = (workspace / "中文完整笔记.md").read_text(encoding="utf-8")
        briefing = (workspace / "讲书稿.md").read_text(encoding="utf-8")
        errors, _warnings = validate_content_map(content, transcript)
        errors += validate_summary_map(summary, briefing, content, notes)
        if not coverage_report(content, summary, transcript).get("passed"):
            errors.append("合同修复后剩余内容覆盖无效")
        corrections = workspace / "editorial_corrections.json"
        if corrections.exists():
            known_ids = {f"{u['id']}-C{i:02d}" for u in content["units"]
                         for i, _ in enumerate(u.get("claims", []), 1)}
            errors += validate_editorial_corrections(load_json(corrections), known_ids)
        if errors:
            raise ValueError("; ".join(errors[:10]))
        for name, before in snapshots.items():
            if name not in REPAIR_FILES:
                path = workspace / name
                if (path.read_bytes() if path.exists() else None) != before:
                    raise RuntimeError("合同修复不得修改原始输入")
        # Compare the entire read set before committing; never fold concurrent
        # edits into a freshly rebound package. Provider failures leave originals intact.
        for name, before in snapshots.items():
            path = folder / name
            if (path.read_bytes() if path.exists() else None) != before:
                raise RuntimeError("合同修复输入在运行期间发生变化")
        try:
            for name in REPAIR_FILES:
                path = workspace / name
                if path.exists() and path.read_bytes() != snapshots.get(name):
                    atomic_write_bytes(folder / name, path.read_bytes())
        except Exception:
            for name in REPAIR_FILES:
                before = snapshots.get(name)
                if before is not None:
                    atomic_write_bytes(folder / name, before)
                elif (folder / name).exists():
                    (folder / name).unlink()
            raise
    return {"action": "repair_review_contract_content", "target_count": len(targets),
            "sources": sources, "rejected_sources": rejected, "removed_claims": removed,
            "affected_units": sorted({t["parent_claim_id"].split("-", 1)[0] for t in targets})}

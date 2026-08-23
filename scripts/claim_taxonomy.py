"""Claim provenance, assertion shape, and compatibility rules for AI review."""
import re


AI_REVIEW_SCHEMA_VERSION = 3

LEGACY_CLAIM_TYPES = (
    "public_fact",
    "guest_firsthand",
    "guest_opinion",
    "editorial_fact",
    "editorial_inference",
    "not_applicable",
)

CLAIM_ORIGINS = (
    "speaker_firsthand",
    "speaker_reported",
    "external_source",
    "editorial_added",
    "episode_metadata",
)

SPEAKER_ROLES = (
    "guest",
    "host",
    "quoted_third_party",
    "editorial",
    "not_applicable",
    "unknown",
)

ASSERTION_TYPES = (
    "fact",
    "opinion",
    "prediction",
    "recommendation",
    "explanation",
    "definition",
    "anecdote",
    "allegation",
    "inference",
)

VERIFICATION_MODES = (
    "web_required",
    "source_document_required",
    "web_spot_check",
    "transcript_attribution",
    "transcript_only",
    "safety_cross_check",
    "not_applicable",
)

RISK_DOMAINS = (
    "general",
    "medical",
    "legal",
    "financial",
    "political",
    "safety",
)

VERDICTS = (
    "supported",
    "qualified",
    "unsupported",
    "contradicted",
    "faithfully_attributed",
    "accurately_reported",
    "not_applicable",
    "uncertain",
)

PUBLICATION_STATUSES = (
    "used_as_fact",
    "attributed_or_qualified",
    "excluded",
)


_ATOMIC_SUBCLAIM_RE = re.compile(r"^(U\d{4}-C\d{2})-F(\d{2})$")


def derive_legacy_claim_type(item):
    """Derive the v2 compatibility bucket from the orthogonal v3 fields."""
    origin = item.get("claim_origin")
    role = item.get("speaker_role")
    assertion = item.get("assertion_type")
    if origin == "speaker_firsthand" and role == "guest" and assertion in {
            "fact", "anecdote"}:
        return "guest_firsthand"
    if role == "guest" and assertion in {
            "opinion", "prediction", "recommendation"}:
        return "guest_opinion"
    if origin == "external_source" and assertion == "fact":
        return "public_fact"
    if origin == "editorial_added" and assertion == "fact":
        return "editorial_fact"
    if origin == "editorial_added" and assertion == "inference":
        return "editorial_inference"
    return "not_applicable"


def atomic_subclaim_parent(subclaim_id):
    match = _ATOMIC_SUBCLAIM_RE.fullmatch(str(subclaim_id or ""))
    return match.group(1) if match else None


def is_cacheable_fact_check(item):
    """Only externally checkable objective facts belong in the web cache."""
    if "claim_origin" in item or "assertion_type" in item:
        if item.get("assertion_type") != "fact":
            return False
        return item.get("claim_origin") in {
            "external_source", "editorial_added"}
    # AI review v2 compatibility during gradual re-review migration.
    return item.get("claim_type") in {"public_fact", "editorial_fact"}

def normalize_review_fact_checks(review):
    """Mechanically align compatibility fields with orthogonal v3 semantics.

    For speaker_reported facts, ``verdict`` evaluates whether the program's
    report is faithfully represented. Any external disagreement remains in
    notes/source_urls rather than turning the attributed speech report itself
    into an unsupported editorial fact.
    """
    changes = []
    for item in review.get("fact_checks", []) or []:
        if not isinstance(item, dict):
            continue
        subclaim = item.get("subclaim_id", "")
        expected_type = derive_legacy_claim_type(item)
        if item.get("claim_type") != expected_type:
            changes.append({
                "subclaim_id": subclaim,
                "field": "claim_type",
                "before": item.get("claim_type"),
                "after": expected_type,
            })
            item["claim_type"] = expected_type

        origin = item.get("claim_origin")
        assertion = item.get("assertion_type")
        status = item.get("publication_status")
        verdict = item.get("verdict")
        segments = item.get("evidence_segment_ids") or []
        if (
                origin == "speaker_reported"
                and assertion == "fact"
                and status == "attributed_or_qualified"
                and segments
                and verdict in {
                    "supported", "unsupported", "contradicted",
                    "faithfully_attributed", "not_applicable",
                }):
            changes.append({
                "subclaim_id": subclaim,
                "field": "verdict",
                "before": verdict,
                "after": "accurately_reported",
            })
            item["verdict"] = "accurately_reported"
        elif (
                origin == "speaker_firsthand"
                and status == "attributed_or_qualified"
                and segments
                and verdict == "supported"):
            changes.append({
                "subclaim_id": subclaim,
                "field": "verdict",
                "before": verdict,
                "after": "faithfully_attributed",
            })
            item["verdict"] = "faithfully_attributed"
    return changes


def validate_review_fact_checks(review, valid_claim_ids=None):
    """Validate AI review v3 fact checks with one executable policy."""
    fact_checks = review.get("fact_checks", [])
    errors = []
    warnings = []
    required = {
        "claim", "parent_claim_id", "subclaim_id", "claim_type",
        "claim_origin", "speaker_role", "assertion_type",
        "verification_mode", "risk_domain", "verdict",
        "publication_status", "evidence_segment_ids", "source_urls",
        "checked_at", "notes",
    }
    seen_subclaims = set()
    subclaim_numbers = {}
    compound_pattern = re.compile(
        r"；|，(?:但|而|因此|从而|同时|并且)|以及|并认为|并称")
    specialized = {
        "opinion", "prediction", "recommendation", "explanation",
        "definition", "allegation",
    }
    high_risk = {"medical", "legal", "financial", "safety"}

    if not isinstance(fact_checks, list):
        return ["AI 审查缺少可复现的 fact_checks"], []
    for index, item in enumerate(fact_checks):
        if not isinstance(item, dict):
            errors.append(f"AI fact_checks[{index}] 必须是对象")
            continue
        claim = item.get("claim") or f"fact_checks[{index}]"
        missing = sorted(required - set(item))
        if missing:
            errors.append(f"{claim}: AI review v3 缺少字段 {missing}")
            continue
        parent = item.get("parent_claim_id")
        subclaim = item.get("subclaim_id")
        parsed_parent = atomic_subclaim_parent(subclaim)
        if parsed_parent != parent:
            errors.append(
                f"{claim}: subclaim_id 必须使用 {{parent_claim_id}}-Fxx")
        if subclaim in seen_subclaims:
            errors.append(f"{claim}: subclaim_id 重复: {subclaim}")
        seen_subclaims.add(subclaim)
        if parsed_parent:
            number = int(str(subclaim).rsplit("F", 1)[1])
            subclaim_numbers.setdefault(parent, []).append(number)
        if valid_claim_ids is not None and parent not in valid_claim_ids:
            errors.append(f"{claim}: parent_claim_id 不存在于 content_map: {parent}")

        expected_legacy = derive_legacy_claim_type(item)
        if item.get("claim_type") != expected_legacy:
            errors.append(
                f"{claim}: claim_type 应由 v3 维度派生为 "
                f"{expected_legacy!r}，实际为 {item.get('claim_type')!r}")

        origin = item.get("claim_origin")
        role = item.get("speaker_role")
        assertion = item.get("assertion_type")
        mode = item.get("verification_mode")
        risk = item.get("risk_domain")
        verdict = item.get("verdict")
        status = item.get("publication_status")
        segments = item.get("evidence_segment_ids") or []
        urls = item.get("source_urls") or []

        if origin in {"speaker_firsthand", "speaker_reported"} and role in {
                "editorial", "not_applicable"}:
            errors.append(f"{claim}: speaker 来源必须填写真实 speaker_role")
        if origin == "editorial_added" and role != "editorial":
            errors.append(f"{claim}: editorial_added 必须使用 speaker_role=editorial")
        if verdict in {"unsupported", "contradicted"} and status == "used_as_fact":
            errors.append(f"{claim}: 未获支持或被反驳内容仍作为事实采用")
        if verdict in {
                "faithfully_attributed", "accurately_reported", "not_applicable"} \
                and status == "used_as_fact":
            errors.append(f"{claim}: 该 verdict 不能作为无归因客观事实采用")

        if origin == "speaker_firsthand":
            if assertion not in specialized and mode != "transcript_attribution":
                errors.append(
                    f"{claim}: speaker_firsthand 核查模式必须是 transcript_attribution")
            if status == "used_as_fact":
                errors.append(f"{claim}: 一手信息必须明确归因")
            if status != "excluded" and not segments:
                errors.append(f"{claim}: 一手信息缺少 transcript segment")
            if (
                    assertion not in specialized
                    and status != "excluded"
                    and verdict not in {"faithfully_attributed", "qualified"}):
                errors.append(f"{claim}: 一手信息 verdict 不符合归因规则")

        if origin == "speaker_reported" and assertion == "fact":
            if status == "used_as_fact":
                errors.append(f"{claim}: speaker_reported 必须明确归因，不能作为无归因事实")
            if status != "excluded" and not segments:
                errors.append(f"{claim}: speaker_reported 缺少 transcript segment")
            if status != "excluded" and verdict not in {
                    "accurately_reported", "qualified", "uncertain"}:
                errors.append(f"{claim}: speaker_reported verdict 不符合来源转述语义")

        if assertion in {"opinion", "prediction"}:
            if status == "used_as_fact":
                errors.append(f"{claim}: 观点或预测不能升级为客观事实")
            if status != "excluded" and not segments:
                errors.append(f"{claim}: 观点或预测缺少转录归因证据")
            if mode not in {
                    "transcript_attribution", "transcript_only", "not_applicable"}:
                errors.append(f"{claim}: 观点或预测不应要求外部事实证明")

        if assertion == "recommendation":
            if origin in {"speaker_firsthand", "speaker_reported"} \
                    and status != "excluded" and not segments:
                errors.append(f"{claim}: 建议缺少说话人转录证据")
            if risk in high_risk and status != "excluded":
                if mode != "safety_cross_check":
                    errors.append(f"{claim}: 高风险建议必须 safety_cross_check")
                if not urls:
                    errors.append(f"{claim}: 高风险建议缺少公开安全核查来源")
            elif status != "excluded" and mode not in {
                    "transcript_attribution", "transcript_only", "not_applicable"}:
                errors.append(f"{claim}: 普通建议核查模式不匹配")

        if assertion in {"explanation", "definition"}:
            if mode not in {
                    "transcript_attribution", "transcript_only",
                    "web_spot_check", "safety_cross_check"}:
                errors.append(f"{claim}: 解释或定义的核查模式不匹配")
            if origin in {"speaker_firsthand", "speaker_reported"} \
                    and status != "excluded" and not segments:
                errors.append(f"{claim}: 解释或定义缺少转录证据")

        if assertion == "allegation":
            if mode != "source_document_required":
                errors.append(f"{claim}: allegation 必须 source_document_required")
            if status != "excluded" and not urls:
                errors.append(f"{claim}: allegation 缺少来源文件 URL")
            if status == "used_as_fact":
                errors.append(f"{claim}: 未裁判指控不能写成既定事实")
            if status != "excluded" and verdict not in {
                    "accurately_reported", "qualified", "unsupported",
                    "contradicted", "uncertain"}:
                errors.append(f"{claim}: allegation verdict 不符合来源转述语义")

        if origin in {"external_source", "editorial_added"} \
                and assertion == "fact" and status == "used_as_fact":
            if mode != "web_required":
                errors.append(f"{claim}: 外部或编辑部客观事实必须 web_required")
            if verdict not in {"supported", "qualified"}:
                errors.append(
                    f"{claim}: 作为事实采用时 verdict 必须为 supported 或 qualified")
            if not urls:
                errors.append(f"{claim}: 外部或编辑部客观事实缺少网页来源")
        if origin == "episode_metadata" and mode != "transcript_only":
            errors.append(f"{claim}: episode_metadata 应使用 transcript_only")
        if assertion == "inference" and status == "used_as_fact":
            errors.append(f"{claim}: 推论必须明确限定，不能写成既定事实")
        if compound_pattern.search(str(claim)):
            warnings.append(
                f"{subclaim}: 子主张仍含复合连接词，请确认已保持单一 assertion_type")

    for parent, numbers in subclaim_numbers.items():
        ordered = sorted(set(numbers))
        expected = list(range(1, len(ordered) + 1))
        if ordered != expected:
            errors.append(
                f"{parent}: subclaim_id 序号必须从 F01 连续递增，实际 {ordered}")
    return errors, warnings

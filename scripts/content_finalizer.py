"""Canonical finalization for notes, briefing, summary map, and TTS text."""
import copy
import re
from pathlib import Path

from scripts.atomic_io import atomic_write_json, atomic_write_text
from scripts.content_map import deduplicate_summary_units, normalize_summary_claim_ids
from scripts.tts import apply_tts_lexicon, load_tts_lexicon, normalize_for_tts
from scripts.validator import (
    MAX_CHAPTER_CHARS,
    MIN_CHAPTER_CHARS,
    audit_narration_issues,
    normalize_briefing_artifacts,
    structure_report,
)


class ContentFinalizationError(RuntimeError):
    """Raised when generated artifacts cannot be safely synchronized."""


def _chapter_titles(text):
    return [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", text or "")
    ]


def _zh_chars(text):
    return len(re.findall(r"[一-鿿]", text or ""))


def _chapter_blocks(text):
    """Return preamble plus ordered chapter title/body pairs."""
    preamble = []
    chapters = []
    current = None
    for line in (text or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = {"title": match.group(1).strip(), "lines": []}
            chapters.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
    return "\n".join(preamble).strip(), [
        {
            "title": chapter["title"],
            "body": "\n".join(chapter["lines"]).strip(),
        }
        for chapter in chapters
    ]


def _claim_unit_id(claim_id, unit_ids):
    claim_id = str(claim_id or "")
    for unit_id in unit_ids:
        if claim_id.startswith(f"{unit_id}-C"):
            return unit_id
    return None


def _safe_split_chapter(block, mapping):
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", block["body"])
        if paragraph.strip()
    ]
    unit_ids = list(mapping.get("unit_ids", []) or [])
    claim_ids = list(mapping.get("claim_ids", []) or [])
    if len(paragraphs) < 2 or len(unit_ids) < 2:
        return None

    unit_split = len(unit_ids) // 2
    left_units = unit_ids[:unit_split]
    right_units = unit_ids[unit_split:]
    if not left_units or not right_units:
        return None

    claims_by_unit = {unit_id: [] for unit_id in unit_ids}
    for claim_id in claim_ids:
        unit_id = _claim_unit_id(claim_id, unit_ids)
        if unit_id is None:
            return None
        claims_by_unit[unit_id].append(claim_id)

    target_ratio = unit_split / len(unit_ids)
    total_chars = sum(max(1, _zh_chars(paragraph)) for paragraph in paragraphs)
    candidates = []
    consumed = 0
    for index, paragraph in enumerate(paragraphs[:-1], start=1):
        consumed += max(1, _zh_chars(paragraph))
        left_body = "\n\n".join(paragraphs[:index])
        right_body = "\n\n".join(paragraphs[index:])
        left_chars = _zh_chars(left_body)
        right_chars = _zh_chars(right_body)
        if left_chars < MIN_CHAPTER_CHARS or right_chars < MIN_CHAPTER_CHARS:
            continue
        candidates.append((
            abs((consumed / total_chars) - target_ratio),
            index,
            left_body,
            right_body,
        ))
    if not candidates:
        return None
    _distance, _index, left_body, right_body = min(candidates)
    if (
            _zh_chars(left_body) > MAX_CHAPTER_CHARS
            or _zh_chars(right_body) > MAX_CHAPTER_CHARS):
        return None

    title = block["title"]
    left_mapping = copy.deepcopy(mapping)
    right_mapping = copy.deepcopy(mapping)
    left_mapping.update({
        "title": f"{title}：上篇",
        "unit_ids": left_units,
        "claim_ids": [
            claim_id
            for unit_id in left_units
            for claim_id in claims_by_unit[unit_id]
        ],
    })
    right_mapping.update({
        "title": f"{title}：下篇",
        "unit_ids": right_units,
        "claim_ids": [
            claim_id
            for unit_id in right_units
            for claim_id in claims_by_unit[unit_id]
        ],
    })
    for chapter in (left_mapping, right_mapping):
        chapter.pop("body_sha256", None)
    return (
        [
            {"title": left_mapping["title"], "body": left_body},
            {"title": right_mapping["title"], "body": right_body},
        ],
        [left_mapping, right_mapping],
    )


def rebalance_long_chapters(briefing_text, summary_map):
    """Split only chapters with safe paragraph and evidence-map boundaries.

    The function refuses to guess when a long chapter has only one natural
    paragraph/unit or when either resulting half would still violate the
    chapter-size limit.
    """
    result = synchronize_summary_chapters(briefing_text, summary_map)
    preamble, blocks = _chapter_blocks(briefing_text)
    mappings = result.get("chapters", [])
    output_blocks = []
    output_mappings = []
    changed = False
    for block, mapping in zip(blocks, mappings, strict=True):
        if _zh_chars(block["body"]) <= MAX_CHAPTER_CHARS:
            output_blocks.append(block)
            output_mappings.append(mapping)
            continue
        split = _safe_split_chapter(block, mapping)
        if split is None:
            raise ContentFinalizationError(
                "无法安全拆分超长章节："
                f"{block['title']}（{_zh_chars(block['body'])} 个中文字符）；"
                "需要至少两个自然段、两个连续 unit，且拆分后每章保持 "
                f"{MIN_CHAPTER_CHARS}–{MAX_CHAPTER_CHARS} 字"
            )
        split_blocks, split_mappings = split
        output_blocks.extend(split_blocks)
        output_mappings.extend(split_mappings)
        changed = True

    if not changed:
        return briefing_text, result, False
    parts = []
    if preamble:
        parts.append(preamble)
    parts.extend(
        f"## {block['title']}\n\n{block['body']}"
        for block in output_blocks
    )
    result["chapters"] = output_mappings
    return "\n\n".join(parts).strip() + "\n", result, True


def _merge_trailing_conclusion(chapters):
    if len(chapters) < 2:
        return False
    if not all(isinstance(chapter, dict) for chapter in chapters):
        raise ContentFinalizationError("summary_map 章节必须是对象")
    trailing = str(chapters[-1].get("title", ""))
    if not re.search(r"结语|结论|总结|回到主线|谨慎乐观", trailing):
        return False
    left = chapters[-2]
    right = chapters[-1]
    for key in ("unit_ids", "claim_ids"):
        left[key] = list(dict.fromkeys(
            list(left.get(key, []) or []) + list(right.get(key, []) or [])
        ))
    chapters.pop()
    return True


def _realign_summary_to_titles(chapters, titles):
    """Recover contiguous chapter merges only; never guess evidence allocation."""
    if not all(isinstance(chapter, dict) for chapter in chapters):
        raise ContentFinalizationError("summary_map 章节必须是对象")
    old_titles = [chapter.get("title") for chapter in chapters]
    if (not titles or len(set(old_titles)) != len(old_titles)
            or len(set(titles)) != len(titles)
            or any(title not in old_titles for title in titles)):
        raise ContentFinalizationError("无法安全重排 summary_map：需要唯一且匹配的章节标题")
    anchors = [old_titles.index(title) for title in titles]
    if anchors[0] != 0 or anchors != sorted(anchors):
        raise ContentFinalizationError("无法安全重排 summary_map：章节顺序不一致")
    rebuilt = []
    for index, start in enumerate(anchors):
        end = anchors[index + 1] if index + 1 < len(anchors) else len(chapters)
        chapter = copy.deepcopy(chapters[start])
        for key in ("unit_ids", "claim_ids"):
            chapter[key] = list(dict.fromkeys(
                item for source in chapters[start:end]
                for item in (source.get(key) or [])
            ))
        chapter.pop("body_sha256", None)
        rebuilt.append(chapter)
    return rebuilt


def synchronize_summary_chapters(briefing_text, summary_map):
    """Align summary chapter titles to the canonical briefing order."""
    result = normalize_summary_claim_ids(copy.deepcopy(summary_map))
    chapters = result.get("chapters")
    if not isinstance(chapters, list):
        raise ContentFinalizationError("summary_map.chapters 必须是数组")
    titles = _chapter_titles(briefing_text)
    while len(chapters) > len(titles) and _merge_trailing_conclusion(chapters):
        pass
    if len(chapters) != len(titles):
        # Recovery path: fragment merges can desync title-keyed summary maps.
        # Rebuild to match the briefing titles while preserving evidence IDs.
        chapters = _realign_summary_to_titles(chapters, titles)
        result["chapters"] = chapters
    for title, chapter in zip(titles, chapters, strict=True):
        if not isinstance(chapter, dict):
            raise ContentFinalizationError("summary_map 章节必须是对象")
        chapter["title"] = title
        chapter.pop("body_sha256", None)
    return result


def repair_summary_structure(briefing_text, summary_map, content_map=None):
    """Merge contiguous duplicate/empty chapters without discarding any prose.

    A unit cannot be divided across chapters under the summary contract. Merge
    the entire intervening span rather than moving its claims away from prose.
    Missing unit IDs can be recovered from explicitly authored claim IDs.
    """
    result = synchronize_summary_chapters(briefing_text, summary_map)
    preamble, blocks = _chapter_blocks(briefing_text)
    chapters = result["chapters"]
    for chapter in chapters:
        units = chapter.setdefault("unit_ids", [])
        for cid in chapter.get("claim_ids", []) or []:
            uid = str(cid).split("-C", 1)[0]
            if re.fullmatch(r"U\d+", uid) and uid not in units:
                units.append(uid)
        if content_map is not None and not chapter.get("claim_ids"):
            chapter["claim_ids"] = [
                f"{unit['id']}-C{i:02d}"
                for unit in content_map.get("units", [])
                if unit.get("id") in units and unit.get("status") != "excluded"
                for i, _ in enumerate(unit.get("claims", []), 1)
            ]
    if not any(chapter.get("unit_ids") for chapter in chapters):
        # No evidence anchor at all: merging cannot repair this package.
        return briefing_text, result
    groups = [[index] for index in range(len(chapters))]
    while len(groups) > 1:
        owner = {}
        span = None
        for index, group in enumerate(groups):
            units = {uid for j in group for uid in chapters[j].get("unit_ids", [])}
            if not units or (content_map is not None and not any(
                    chapters[j].get("claim_ids") for j in group)):
                span = (max(0, index - 1), max(1, index))
                break
            for uid in sorted(units):
                if uid in owner:
                    span = (owner[uid], index)
                    break
                owner[uid] = index
            if span:
                break
        if span is None:
            break
        start, stop = span
        groups[start:stop+1] = [sum(groups[start:stop+1], [])]
    merged = []
    output = []
    for group in groups:
        mapping = copy.deepcopy(chapters[group[0]])
        for field in ("unit_ids", "claim_ids", "number_ids", "example_ids"):
            if any(field in chapters[j] for j in group):
                mapping[field] = list(dict.fromkeys(
                    value for j in group for value in chapters[j].get(field, []) or []))
        merged.append(mapping)
        output.append(f"## {mapping['title']}\n\n" + "\n\n".join(
            blocks[j]["body"] for j in group))
    result["chapters"] = merged
    text = "\n\n".join(([preamble] if preamble else []) + output).strip() + "\n"
    try:
        text, result, _split = rebalance_long_chapters(text, result)
    except ContentFinalizationError:
        # A single indivisible unit may be too long to merge safely. Preserve
        # the authored package for bounded writer feedback, never lower depth
        # limits or discard content to make bookkeeping pass.
        return briefing_text, synchronize_summary_chapters(briefing_text, summary_map)
    return text, result


def finalize_content_artifacts(briefing_text, summary_map):
    """Canonicalize briefing text and keep summary bindings synchronized."""
    aligned = synchronize_summary_chapters(briefing_text, summary_map)
    deduplicated = deduplicate_summary_units(aligned)
    finalized, aligned, changes = normalize_briefing_artifacts(
        briefing_text, aligned)
    if deduplicated:
        changes.append("deduplicate_summary_units")
    aligned = synchronize_summary_chapters(finalized, aligned)
    finalized, aligned, split_long = rebalance_long_chapters(
        finalized, aligned)
    if split_long:
        changes.append("split_long_chapters")
    finalized, aligned, post_split_changes = normalize_briefing_artifacts(
        finalized, aligned)
    for change in post_split_changes:
        if change not in changes:
            changes.append(change)
    aligned = synchronize_summary_chapters(finalized, aligned)
    # synchronize_summary_chapters intentionally removes stale hashes; refresh
    # them once more after titles and chapter bodies have reached final form.
    finalized, aligned, hash_changes = normalize_briefing_artifacts(
        finalized, aligned)
    for change in hash_changes:
        if change not in changes:
            changes.append(change)
    return finalized, aligned, changes


def generate_safe_tts_lexicon(text, existing=None):
    """Generate only deterministic acronym pronunciations.

    Proper names remain review-gated; this function intentionally avoids
    guessing pronunciations for mixed-case words such as ImageNet.
    """
    result = dict(existing or {})
    pattern = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,})(\+)?(?![A-Za-z0-9])")
    for match in pattern.finditer(text or ""):
        acronym = match.group(1)
        source = acronym + ("+" if match.group(2) else "")
        if source in result:
            continue
        spoken = " ".join(acronym)
        if match.group(2):
            spoken += " 加"
        result[source] = spoken
    return result


def _spoken_integer(value):
    digits = {
        "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
        "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
    }
    text = str(value).strip()
    if all(character in digits for character in text):
        return "".join(digits[character] for character in text)
    return text


def validate_tts_lexicon_semantics(lexicon):
    """Reject deterministic pronunciation mappings that change fractions."""
    issues = []
    fraction = re.compile(
        r"([0-9一二三四五六七八九十]+)\s*[/／]\s*"
        r"([0-9一二三四五六七八九十]+)"
    )
    for source, spoken in (lexicon or {}).items():
        matches = list(fraction.finditer(str(source)))
        for match in matches:
            numerator = _spoken_integer(match.group(1))
            denominator = _spoken_integer(match.group(2))
            expected = f"{denominator}分之{numerator}"
            if expected not in str(spoken):
                issues.append(
                    f"TTS 词典分数读音错误: {source!r} 应包含 {expected!r}")
    return issues


def validate_tts_readiness(text, lexicon=None):
    """Validate the exact text produced by normalization plus lexicon use."""
    lexicon = lexicon or {}
    spoken = apply_tts_lexicon(normalize_for_tts(text or ""), lexicon)
    issues = validate_tts_lexicon_semantics(lexicon)
    numbers = sorted(set(re.findall(r"\d[\d,.%]*", spoken)))
    if numbers:
        issues.append(f"TTS 输入仍有阿拉伯数字: {numbers[:10]}")
    acronyms = sorted(set(re.findall(
        r"(?<![A-Za-z0-9])(?:[A-Z]{2,})(?![A-Za-z0-9])", spoken)))
    if acronyms:
        issues.append(f"TTS 输入仍有未映射全大写缩写: {acronyms[:10]}")
    symbols = sorted(set(re.findall(r"[+/−]", spoken)))
    if symbols:
        issues.append(f"TTS 输入仍有难读符号: {symbols}")
    repeated = sorted(set(
        match.group(1)
        for match in re.finditer(
            r"(?<![A-Za-z一-鿿])([A-Za-z]{2,}|[一-鿿]{2,})"
            r"[\s，、；：:]+\1(?![A-Za-z一-鿿])",
            spoken,
            flags=re.IGNORECASE,
        )
    ))
    if repeated:
        issues.append(f"TTS 词典替换后出现重复表达: {repeated[:10]}")
    english_words = []
    for word in re.findall(
            r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9.-]{1,})(?![A-Za-z0-9])",
            spoken):
        # Plain lowercase words and conventional Title Case are normally
        # pronounceable by the bilingual TTS model. Block mixed-case brands,
        # letter/number blends, and punctuation-heavy identifiers whose
        # pronunciation cannot be inferred safely (ImageNet, OpenAI, GPT-4).
        plain_word = bool(
            re.fullmatch(r"[a-z]+", word)
            or re.fullmatch(r"[A-Z][a-z]+", word)
            or re.fullmatch(r"[A-Z]{2,}", word)
        )
        if not plain_word:
            english_words.append(word)
    english_words = sorted(set(english_words))
    if english_words:
        issues.append(f"TTS 输入仍有未确认读音的英文串: {english_words[:10]}")
    return issues


_NOTES_PROVENANCE_RE = re.compile(
    r"^\s*(?:转录依据|依据说明|资料依据)[:：]")
_INTERNAL_FILE_RE = re.compile(
    r"content_map\.json|summary_map\.json|原始转录\.txt|转录_纠错\.txt|来源\.md")
_NOTES_PROVENANCE_HEADING_RE = re.compile(
    r"^##\s*(?:证据基础|转录依据|资料依据|依据说明|来源说明|证据说明)\s*$")


def strip_notes_provenance_preface(text):
    """Drop writer-added provenance bookkeeping before the first real chapter.

    Writers sometimes open 中文完整笔记.md with a line or a "## 证据基础"
    section such as "本笔记依据原始转录.txt ... content_map.json ..."; that is
    internal bookkeeping, not public prose. Only content before the first
    real chapter is touched: preface lines are dropped when they match the
    provenance prefix or cite internal pipeline files, and a provenance-titled
    section is dropped only when every body line is flagged by that audit.
    """
    lines = (text or "").splitlines(keepends=True)
    out = []
    index = 0
    in_preface = True
    while index < len(lines):
        line = lines[index]
        if in_preface and _NOTES_PROVENANCE_HEADING_RE.match(line.strip()):
            stop = index + 1
            while stop < len(lines) and not lines[stop].startswith("## "):
                stop += 1
            body = [item for item in lines[index + 1:stop] if item.strip()]
            if body and all(audit_narration_issues(item) for item in body):
                index = stop
                continue
            in_preface = False
        elif line.startswith("## "):
            in_preface = False
        elif (in_preface and line.strip()
              and any(item.startswith("## ") for item in lines[index + 1:])
              and (_NOTES_PROVENANCE_RE.match(line)
                   or _INTERNAL_FILE_RE.search(line))):
            index += 1
            continue
        out.append(line)
        index += 1
    cleaned = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def finalize_content_package(folder):
    """Finalize generated content files before evidence enrichment/review."""
    folder = Path(folder)
    briefing_path = folder / "讲书稿.md"
    summary_path = folder / "summary_map.json"
    if not briefing_path.exists() or not summary_path.exists():
        raise ContentFinalizationError("缺少讲书稿.md 或 summary_map.json")

    import json
    briefing = briefing_path.read_text(encoding="utf-8")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    content_path = folder / "content_map.json"
    content = (json.loads(content_path.read_text(encoding="utf-8"))
               if content_path.exists() else None)
    briefing, summary = repair_summary_structure(briefing, summary, content)
    briefing, summary, changes = finalize_content_artifacts(
        briefing, summary)
    audit_issues = audit_narration_issues(briefing)
    if audit_issues:
        raise ContentFinalizationError(
            "讲稿包含面向内部的审查过程语言: "
            + "; ".join(audit_issues[:5]))
    notes_path = folder / "中文完整笔记.md"
    if notes_path.exists():
        notes_text = notes_path.read_text(encoding="utf-8")
        cleaned_notes = strip_notes_provenance_preface(notes_text)
        if cleaned_notes != notes_text:
            notes_path.write_text(cleaned_notes, encoding="utf-8")
            notes_text = cleaned_notes
        notes_audit_issues = audit_narration_issues(notes_text)
        if notes_audit_issues:
            raise ContentFinalizationError(
                "完整笔记包含面向内部的审查过程语言: "
                + "; ".join(notes_audit_issues[:5]))
    depth_issues = [warning for warning in structure_report(briefing)
                    if warning.startswith(("碎片章", "超长章", "章均过浅", "章节贴地板",
                                           "未检测到任何"))]
    if depth_issues:
        raise ContentFinalizationError(
            "讲稿章节深度未通过（需按证据补实质论证，不自动凑字）: "
            + "; ".join(depth_issues))
    atomic_write_text(briefing_path, briefing)
    atomic_write_json(summary_path, summary)

    lexicon = generate_safe_tts_lexicon(
        briefing, load_tts_lexicon(folder))
    if lexicon:
        atomic_write_json(folder / "tts_lexicon.json", lexicon)
    return {
        "briefing": briefing,
        "summary_map": summary,
        "normalization_changes": changes,
        "tts_lexicon_entries": len(lexicon),
    }

"""Canonical finalization for notes, briefing, summary map, and TTS text."""
import copy
import re
from pathlib import Path

try:
    from atomic_io import atomic_write_json, atomic_write_text
    from content_map import normalize_summary_claim_ids
    from tts import apply_tts_lexicon, load_tts_lexicon, normalize_for_tts
    from validator import MAX_CHAPTER_CHARS, MIN_CHAPTER_CHARS, normalize_briefing_artifacts
except ImportError:
    from scripts.atomic_io import atomic_write_json, atomic_write_text
    from scripts.content_map import normalize_summary_claim_ids
    from scripts.tts import apply_tts_lexicon, load_tts_lexicon, normalize_for_tts
    from scripts.validator import (
        MAX_CHAPTER_CHARS,
        MIN_CHAPTER_CHARS,
        normalize_briefing_artifacts,
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
    for block, mapping in zip(blocks, mappings):
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
        raise ContentFinalizationError(
            "讲稿与 summary_map 章节数量不一致: "
            f"briefing={len(titles)}, summary={len(chapters)}"
        )
    for title, chapter in zip(titles, chapters):
        if not isinstance(chapter, dict):
            raise ContentFinalizationError("summary_map 章节必须是对象")
        chapter["title"] = title
        chapter.pop("body_sha256", None)
    return result


def finalize_content_artifacts(briefing_text, summary_map):
    """Canonicalize briefing text and keep summary bindings synchronized."""
    aligned = synchronize_summary_chapters(briefing_text, summary_map)
    finalized, aligned, changes = normalize_briefing_artifacts(
        briefing_text, aligned)
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


def validate_tts_readiness(text, lexicon=None):
    """Validate the exact text produced by normalization plus lexicon use."""
    spoken = apply_tts_lexicon(normalize_for_tts(text or ""), lexicon or {})
    issues = []
    numbers = sorted(set(re.findall(r"\d[\d,.%]*", spoken)))
    if numbers:
        issues.append(f"TTS 输入仍有阿拉伯数字: {numbers[:10]}")
    acronyms = sorted(set(re.findall(
        r"(?<![A-Za-z0-9])(?:[A-Z]{2,})(?![A-Za-z0-9])", spoken)))
    if acronyms:
        issues.append(f"TTS 输入仍有未映射全大写缩写: {acronyms[:10]}")
    symbols = sorted(set(re.findall(r"[+/]", spoken)))
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
    briefing, summary, changes = finalize_content_artifacts(
        briefing, summary)
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

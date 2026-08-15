"""
讲稿质量校验与自动修复 + 句子边界切片。
"""
import copy
import hashlib
import re

try:
    from sections import chapter_sections, parse_markdown_sections, preamble_text
except ImportError:
    from scripts.sections import chapter_sections, parse_markdown_sections, preamble_text


def validate_and_fix(text, return_details=False):
    """校验讲稿并执行既有机械修复，可选返回可审计的分类明细。"""
    issues = []
    auto_fixes = []
    warnings = []

    def report(message, *, fixed=False):
        issues.append(message)
        (auto_fixes if fixed else warnings).append(message)

    # 1. 检查 ## 标题数量
    headers = re.findall(r"^## .+", text, re.MULTILINE)
    if len(headers) < 3:
        report(f"标题太少（{len(headers)}个，建议至少 3 个）")

    # 2. 修复 ## ## 双标题（现有内容中发现的 bug）
    if re.search(r"^## ## ", text, re.MULTILINE):
        text = re.sub(r"^## ## ", r"## ", text, flags=re.MULTILINE)
        report("修复了 ## ## 双标题", fixed=True)

    # 3. 修复 ### → ##
    if "###" in text:
        text = text.replace("### ", "## ")
        report("修复了 ### -> ##", fixed=True)

    # 4. 清除 ** 加粗
    if "**" in text:
        text = text.replace("**", "")
        report("清除了 ** 加粗", fixed=True)

    # 5. 清除 * 斜体（单个星号，排除双星号已处理的）
    if re.search(r"(?<!\*)\*(?!\*)", text):
        text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
        report("清除了 * 斜体", fixed=True)

    # 6. 替换破折号
    if "——" in text:
        text = text.replace("——", "，")
        report("替换了破折号", fixed=True)

    # 7. 清除分隔线
    if re.search(r"^---+\s*$", text, re.MULTILINE):
        text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)
        report("清除了分隔线", fixed=True)

    # 8. 清除树形图符号
    for sym in ["├", "└", "│", "━"]:
        if sym in text:
            text = text.replace(sym, "")
            report(f"清除了符号 {sym}", fixed=True)

    # 9. 清除代码块
    if "```" in text:
        text = re.sub(r"```[\s\S]*?```", "", text)
        report("清除了代码块", fixed=True)

    # 10. 检查私人化收尾
    for phrase in ["通勤愉快", "我们下期见", "我们下本书见", "大家好"]:
        if phrase in text:
            text = text.replace(phrase, "")
            report(f"清除了私人化表达：{phrase}", fixed=True)

    # 11. 检查篇幅
    if len(text) < 5000:
        report(f"篇幅偏短（{len(text)}字，建议 >= 5000）")

    if issues:
        summary = [f"[校验] 发现 {len(issues)} 个问题"]
        if auto_fixes:
            summary.append(
                f"已自动修复 {len(auto_fixes)} 项: {'; '.join(auto_fixes)}")
        if warnings:
            summary.append(
                f"仅报告 {len(warnings)} 项: {'; '.join(warnings)}")
        print("；".join(summary), flush=True)
    else:
        print("[校验] 通过，无问题", flush=True)

    if return_details:
        return text, issues, {
            "auto_fixes": auto_fixes,
            "warnings": warnings,
        }
    return text, issues


# ── 结构体检（只报告，不自动修改）────────────────────────────────

# 章节粒度阈值（与 讲稿提示词.md 的"章节粒度"一节保持一致）
MIN_CHAPTER_CHARS = 400      # 碎片章阈值（中文字数，不含标题）
MAX_CHAPTER_CHARS = 1000     # 超长章阈值（中文字数，不含标题）
MAX_CHAPTERS = 25            # 章节数上限（防过度碎片化）
MIN_INTRODUCTION_CHARS = 40  # 引言段最少中文字数


def _zh_chars(s):
    """统计中文字数（含标点外的汉字）。"""
    return len(re.findall(r"[一-鿿]", s))


def _split_chapters(text):
    """Return preamble and chapter sizes from the canonical section model."""
    chapters = [
        (section.title, _zh_chars(section.body))
        for section in chapter_sections(text)
    ]
    return preamble_text(text), chapters


_CN_DIGITS = "零一二三四五六七八九"
_CN_SMALL_UNITS = ("", "十", "百", "千")
_CN_BIG_UNITS = ("", "万", "亿", "兆")


def _four_digit_chinese(value):
    result = []
    pending_zero = False
    for position in range(3, -1, -1):
        digit = value // (10 ** position) % 10
        if digit:
            if pending_zero and result:
                result.append("零")
            result.append(_CN_DIGITS[digit])
            if position:
                result.append(_CN_SMALL_UNITS[position])
            pending_zero = False
        elif result and value % (10 ** position or 1):
            pending_zero = True
    text = "".join(result)
    if text.startswith("一十"):
        text = text[1:]
    return text


def integer_to_chinese(value):
    """Convert a non-negative integer without changing its magnitude."""
    value = int(value)
    if value == 0:
        return "零"
    groups = []
    while value:
        groups.append(value % 10000)
        value //= 10000
    result = []
    pending_zero = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            if result:
                pending_zero = True
            continue
        if result and (pending_zero or group < 1000):
            if result[-1] != "零":
                result.append("零")
        result.append(_four_digit_chinese(group))
        if index:
            result.append(_CN_BIG_UNITS[index])
        pending_zero = False
    return "".join(result)



def _normalize_arabic_numbers(text):
    changed = False
    pattern = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?")

    def replace(match):
        nonlocal changed
        token = match.group(0)
        percent = token.endswith("%")
        core = token[:-1] if percent else token
        compact = core.replace(",", "")
        following = text[match.end():match.end() + 4]
        if "." in compact:
            integer, fraction = compact.split(".", 1)
            spoken = (
                integer_to_chinese(int(integer))
                + "点"
                + "".join(_CN_DIGITS[int(digit)] for digit in fraction)
            )
        elif len(compact) == 4 and re.match(r"\s*年", following):
            spoken = "".join(_CN_DIGITS[int(digit)] for digit in compact)
        else:
            spoken = integer_to_chinese(int(compact))
        changed = True
        return f"百分之{spoken}" if percent else spoken

    return pattern.sub(replace, text), changed


def _chapter_blocks(text):
    sections = parse_markdown_sections(text)
    preamble = "\n\n".join(
        section.body for section in sections if section.title is None
    )
    chapters = [
        {"title": section.title, "body": section.body}
        for section in sections if section.title is not None
    ]
    return preamble, chapters


def _summary_chapter(summary_map, title):
    for chapter in summary_map.get("chapters", []) or []:
        if isinstance(chapter, dict) and chapter.get("title") == title:
            return chapter
    return None


def _merge_summary_chapters(summary_map, left_title, right_title):
    left = _summary_chapter(summary_map, left_title)
    right = _summary_chapter(summary_map, right_title)
    if not left or not right:
        return
    for key in ("unit_ids", "claim_ids"):
        left[key] = list(dict.fromkeys(
            list(left.get(key, []) or []) + list(right.get(key, []) or [])
        ))
    summary_map["chapters"] = [
        chapter
        for chapter in summary_map.get("chapters", [])
        if chapter is not right
    ]


def normalize_briefing_artifacts(text, summary_map):
    """Normalize TTS text and keep summary-map chapter bindings in sync."""
    original = text
    summary_map = copy.deepcopy(summary_map or {"chapters": []})
    changes = []

    text, numbers_changed = _normalize_arabic_numbers(text)
    if numbers_changed:
        changes.append("normalized_numbers")

    compact = re.sub(
        r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])",
        "",
        text,
    )
    if compact != text:
        text = compact
        changes.append("removed_cjk_spaces")

    preamble, chapters = _chapter_blocks(text)
    merged = False
    while len(chapters) > 3:
        fragment_index = next(
            (
                index for index, chapter in enumerate(chapters)
                if _zh_chars(chapter["body"]) < MIN_CHAPTER_CHARS
            ),
            None,
        )
        if fragment_index is None:
            break
        if fragment_index + 1 < len(chapters):
            left_index = fragment_index
            right_index = fragment_index + 1
        else:
            left_index = fragment_index - 1
            right_index = fragment_index
        left = chapters[left_index]
        right = chapters[right_index]
        left["body"] = (left["body"] + "\n\n" + right["body"]).strip()
        _merge_summary_chapters(
            summary_map, left["title"], right["title"])
        chapters.pop(right_index)
        merged = True
    if merged:
        changes.append("merged_fragment_chapters")

    rendered = [preamble] if preamble else []
    rendered.extend(
        f"## {chapter['title']}\n\n{chapter['body']}"
        for chapter in chapters
    )
    text = "\n\n".join(rendered).strip()
    if original.endswith("\n"):
        text += "\n"

    chapter_bodies = {
        chapter["title"]: chapter["body"] for chapter in chapters
    }
    for chapter in summary_map.get("chapters", []) or []:
        if not isinstance(chapter, dict):
            continue
        body = chapter_bodies.get(chapter.get("title"))
        if body is not None:
            chapter["body_sha256"] = hashlib.sha256(
                body.strip().encode("utf-8")
            ).hexdigest()

    if text != original and not changes:
        changes.append("normalized_formatting")
    return text, summary_map, changes


def structure_report(text):
    """讲稿内容结构体检。返回 warning 列表，只报告、不修改内容。

    检查项：
      1. 引言段缺失或过短
      2. 章节粒度（碎片章 <400 / 超长章 >1000）
      3. 章节数过多（>25）
      4. 正文残留 SPEAKER_XX 标签
      5. 中文间夹英文空格（如 "是 一家"）
      6. 章节标题带括号/编号
    """
    warns = []
    preamble, chapters = _split_chapters(text)

    # 1. 引言段
    if not preamble:
        warns.append("引言段缺失（第一个 ## 前需 2–3 句全局导览）")
    elif _zh_chars(preamble) < MIN_INTRODUCTION_CHARS:
        warns.append(
            f"引言段过短（{_zh_chars(preamble)}字，建议 50–100 字）")

    # 2-3. 章节粒度 + 章节数
    if not chapters:
        warns.append("未检测到任何 ## 章节")
    else:
        for title, n in chapters:
            if n < MIN_CHAPTER_CHARS:
                warns.append(f"碎片章（{n}字 < {MIN_CHAPTER_CHARS}）：{title}")
            elif n > MAX_CHAPTER_CHARS:
                warns.append(f"超长章（{n}字 > {MAX_CHAPTER_CHARS}）：{title}")
        if len(chapters) > MAX_CHAPTERS:
            warns.append(
                f"章节数偏多（{len(chapters)} 章 > {MAX_CHAPTERS}），注意粒度")

    # 4. SPEAKER 残留
    if re.search(r"SPEAKER_\d+", text):
        warns.append("正文残留 SPEAKER_XX 说话人标签，需替换为身份描述")

    # 5. 中文间夹空格（只匹配行内空格，排除 标题/段落 的换行边界）
    gaps = re.findall(r"[一-鿿][ \t]+[一-鿿]", text)
    if gaps:
        warns.append(f"中文间夹空格 {len(gaps)} 处（如 '是 一家'），需合并")

    # 6. 标题带括号/编号
    for title, _ in chapters:
        if re.search(r"[（(][^）)]*[）)]|第\d+[章节篇]|^\d+[\.、]", title):
            warns.append(f"标题带括号或编号，需改写：{title}")

    return warns


def smart_chunk(text, max_chars=800):
    """按句子边界切割文本，不在句子中间断开。"""
    sentences = re.split(r"(?<=[。！？\.\!\?])\s*", text.strip())
    chunks = []
    current = ""

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(s) > max_chars:
            # 单句超长则硬切
            if current:
                chunks.append(current)
                current = ""
            for j in range(0, len(s), max_chars):
                chunks.append(s[j:j + max_chars])
            continue
        if len(current) + len(s) + 1 <= max_chars:
            current = (current + " " + s).strip() if current else s
        else:
            if current:
                chunks.append(current)
            current = s

    if current:
        chunks.append(current)
    return chunks

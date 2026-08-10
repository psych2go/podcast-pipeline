"""
讲稿质量校验与自动修复 + 句子边界切片。
"""
import re


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
    """按 ## 章节拆分。返回 (preamble, [(title, body_zh_chars), ...])。"""
    parts = re.split(r"\n(?=## )", text.strip())
    preamble = ""
    chapters = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("## "):
            lines = p.split("\n", 1)
            title = lines[0].replace("## ", "").strip()
            body = lines[1] if len(lines) > 1 else ""
            chapters.append((title, _zh_chars(body)))
        elif not chapters:
            preamble = p
    return preamble, chapters


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

"""
[LEGACY] DeepSeek 讲稿生成模块（大纲驱动 + LLM 说话人检测 + 动态篇幅控制）。

⚠ v4 起，讲稿生成改由 Claude Code 终端在对话中直接完成（参考 scripts/讲稿提示词.md）。
本模块不再被 process.py 调用，保留仅供回溯 / 未来若要恢复 API 自动生成时复用。
其 get_system_prompt() / generate_briefing() 的逻辑仍是 scripts/讲稿提示词.md 的内容来源。
"""
import sys
from pathlib import Path

# Ensure scripts/ is in sys.path for direct execution (legacy/ 的父目录即 scripts/)
_scripts = str(Path(__file__).resolve().parent.parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import re

import httpx
from tqdm import tqdm

from config import DEEPSEEK_KEY, DS_MODEL, LLM_BASE_URL


# ── DeepSeek API 调用 ─────────────────────────────────────────────

def call_deepseek(system_prompt, user_msg, temperature=0.5, max_tokens=16000):
    payload = {
        "model": DS_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=httpx.Timeout(300, connect=30),
    )
    if r.status_code != 200:
        raise RuntimeError(f"DeepSeek {r.status_code}: {r.text[:200]}")
    msg = r.json()["choices"][0]["message"]
    # content 是最终答案；reasoning_content 是推理模型的思维链（CoT），不是答案。
    # 优先取 content，避免把原始思考过程当成讲稿返回。
    return msg.get("content", "") or msg.get("reasoning_content", "") or ""


# ── LLM 说话人检测（替代硬编码名单）────────────────────────────────

def estimate_speakers_via_llm(transcript):
    """用 DeepSeek 分析转录文本，返回说话人数量（主要方法）。"""
    prompt = (
        "Analyze the following podcast transcript excerpt and determine "
        "how many distinct speakers (people talking) there are.\n"
        "Reply with ONLY a single integer, no other text.\n\n"
        f"{transcript[:3000]}"
    )
    try:
        result = call_deepseek(
            "You are a helpful assistant that counts speakers in transcripts. "
            "Reply only with a number.",
            prompt,
            temperature=0.1,
            max_tokens=200,
        )
        count = int(result.strip())
        return max(1, min(count, 20))
    except Exception as e:
        print(f"[分析] LLM 说话人检测失败: {e}，回退到启发式方法", flush=True)
        return None  # caller falls back


def estimate_speakers_heuristic(transcript):
    """启发式方法：硬编码名字 + 对话特征（fallback）。"""
    speakers = set()
    # 常见说话人名样例——按你常处理的播客自行增补
    known_names = [
        r"Host", r"Guest", r"Speaker_\d+", r"主持人",
        r"Narrator", r"Interviewer", r"Co[-_]?host",
    ]
    for pattern in known_names:
        if re.search(pattern, transcript, re.IGNORECASE):
            speakers.add(pattern)
    if len(speakers) >= 3:
        return len(speakers)
    questions = len(re.findall(r"[?？]", transcript[:5000]))
    if questions > 30:
        return max(3, len(speakers))
    return max(1, len(speakers))


def estimate_speaker_count(transcript):
    """主入口：先用 LLM，失败回退到启发式。"""
    count = estimate_speakers_via_llm(transcript)
    if count is not None:
        print(f"[分析] LLM 检测到 {count} 位说话人", flush=True)
        return count
    count = estimate_speakers_heuristic(transcript)
    print(f"[分析] 启发式检测到 {count} 位说话人", flush=True)
    return count


# ── 动态篇幅 ───────────────────────────────────────────────────────

def calc_target_chars(transcript_len):
    """根据原文长度动态计算目标篇幅（默认 30%，封顶 60K）。"""
    target = max(8000, int(transcript_len * 0.30))
    return min(target, 60000)


# ── 提示词 ─────────────────────────────────────────────────────────

def get_system_prompt(speaker_count, target_chars):
    """根据说话人数和目标篇幅生成提示词。"""
    is_multi = speaker_count > 2

    lines = [
        "你是一位资深播客讲书人。请将以下播客转写稿，改写为一份用于 TTS 收听的中文讲书稿。",
        "",
        "你的身份是一位敏锐的内容提炼者。你不是在现场复述对话，而是听完之后，把最有价值的信息用清晰的口语讲出来。",
        "",
        "【内容铁律】",
        "- 篇幅由内容决定，不要量化。每个话题充分展开论证过程、背景和推导链条，把原播客的核心内容讲透。",
        "- 100% 保留：核心论点、推导逻辑、具体数据、案例名称、专业术语、底层机制。",
    ]
    if not is_multi:
        lines.append("- 嘉宾背景：嘉宾首次出现时注明其公司、职位或专业领域（角色从对话推断，不能编造）。")
    lines.extend([
        "- 严禁脑补：可以改变叙述语气，但绝对不能编造原播客中没有的案例、数据或比喻。",
        "- 严禁私人化收尾：结尾用最后一个观点自然收束，不要说任何与内容本身无关的话。",
        "",
        "【开场策略 — 根据内容选一种】",
        "- 辩论型内容：以核心分歧开场（如“这期播客里出现了两种截然相反的观点”）。",
        "- 独白/讲述型：以最反直觉的结论开场（如“这期有一个判断让我印象极深”）。",
        "- 多人讨论型：以最有吸引力的悬念开场（如“主持人一上来就抛出一个尖锐的问题”）。",
        "“⚠” 重要：不要使用固定句式。避免“好的朋友”“我刚听完一期播客”“信息量爆炸”这类话术。每期播客的开场应该不同，由内容本身决定。",
        "",
        "【对话梳理】",
        "- 按核心话题组织，相近观点合并，独有案例不可删。",
        "- 同一论点的重复表述合并为一次完整论述。完全跑题的内容直接删掉。",
    ])

    if is_multi:
        lines.extend([
            "",
            "【说话人处理】",
            "- 转述时必须标注说话人身份。首次出现用角色加名字（如 YC 总裁 Gary Tan）。",
            "- 主持人提问和控场不能丢。对立观点明确标注谁反对谁。",
            "- 某人说了很长的观点时，先归纳结论再展开推理过程，不要逐句复述。",
        ])

    lines.extend([
        "",
        "【金句处理】",
        "- 金句独立成段，不加引号。用自然的方式引入（如“原话很精辟”），避免固定套话。",
        "- 禁用命令式引导词（如“记住”“请注意”）。",
        "",
        "【TTS 听觉节奏】",
        "- 段落不要太长：每段 3-5 句为宜，给听众呼吸空间。",
        "- 长短句交错：密集信息（数据、逻辑链）用短句拆开；过渡和背景用稍长的句子。",
        "- 重要数据前加引导语（如“给你一个具体的数字”），避免连续堆砌数字。",
        "- 阐述一个重要观点后，留一句话解释或点评，让听众有时间消化。",
        "",
        "【表达风格】",
        "- 书面口语化，多用短句。",
        "- 段落之间加自然的听觉过渡句，确保听众光靠耳朵就能跟上话题转换。",
        "- 第一个 ## 话题不要做全局开场白，直接进入话题内容。后续话题用一句简短过渡带出即可。",
        "",
        "【输出格式 — TTS 纯文本】",
        "- 章节标题用 ## 开头。正文禁用所有 Markdown 符号（**、*、` 等）。",
        "- 禁用 —— 破折号和 --- 分隔线。",
        "- 英文与中文混合时按朗读习惯归一：专有名词/产品名可保留英文（OpenAI、API、token、Stripe）；但可翻译的常见词要转中文（thoughtful→周到，legacy product→老旧产品，screaming buy→强烈推荐买入）；混合动词表达要改写（miss掉→错过，build出→做出）。",
        "- 数字、货币、百分比写成口语（56 美元而不是 $56；21 倍而不是 21x；百分之五十二而不是 52%）。",
        "- 符号读不出来的都要转文字（&→和，/→斜杠或顿号，=→等于）。",
        "- 最终输出必须是可直接交给 TTS 朗读的纯文本正文。",
    ])

    return "\n".join(lines)


# ── 大纲驱动生成 ───────────────────────────────────────────────────

def generate_outline(transcript):
    """读转录首/中/尾采样，输出话题大纲（用于结构引导）。失败返回 []。"""
    n = len(transcript)
    head = transcript[:6000]
    tail = transcript[-5000:]
    mid = transcript[n // 2 - 2000 : n // 2 + 2000] if n > 16000 else ""

    sample = f"逐字稿（前 6000 字）：\n{head}\n\n"
    if mid:
        sample += f"逐字稿（中段 4000 字）：\n{mid}\n\n"
    sample += f"逐字稿（后 5000 字）：\n{tail}"

    outline_system = (
        "你是一位播客内容分析师。你的任务是从逐字稿中提取核心话题。"
        "每行输出一个简短的标题（15字以内），不要序号，不要解释段落。"
    )
    outline_prompt = (
        "请从以下播客逐字稿采样中提取本期讨论的核心话题（通常 3-12 个）。\n"
        "每行只输出一个标题，不要序号，不要解释。\n\n"
        f"{sample}"
    )
    try:
        r = call_deepseek(outline_system, outline_prompt, max_tokens=2000)
    except Exception as e:
        print(f"[大纲] 生成失败: {e}，跳过大纲", flush=True)
        return []
    topics = [
        t.strip().lstrip("#").lstrip().lstrip("0123456789.-）) ").strip()
        for t in r.strip().split("\n") if t.strip()
    ]
    # 过滤掉太长的行（段落而非标题），但保留合理长度
    topics = [t for t in topics if 3 < len(t) < 50]
    print(f"[大纲] 识别出 {len(topics)} 个话题: {topics}", flush=True)
    return topics


# ── 全文覆盖生成（替代旧的 15000 字截断一次性生成）─────────────────

def chunk_transcript(transcript, chunk_size=None, overlap=1000):
    """按顺序将转录切成带重叠的块，保证全文都被模型读到（不截断丢内容）。"""
    n = len(transcript)
    if chunk_size is None:
        # 自适应：目标约 10-15 块；单块 8K-15K 字符，控制在上下文与调用次数之间
        chunk_size = min(15000, max(8000, n // 12))
    if n <= chunk_size:
        return [transcript]
    chunks = []
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(transcript[start:end])
        if end == n:
            break
        start = end - overlap  # 重叠保证跨块话题不被切断
    return chunks


def _extract_headings(text):
    """提取讲稿中已有的 ## 标题，用于告诉后续分块别重复。"""
    return [m.strip() for m in re.findall(r"^## (.+)$", text, re.MULTILINE)]


def generate_briefing(transcript, system_prompt, target, progress_prefix="[生成]"):
    """
    大纲引导 + 顺序分块续写，确保整篇逐字稿都被读到。

    - 小转录（< 18K 字符）：一次性全文生成（不截断）。
    - 大转录：先出大纲做结构引导，再按顺序分块续写；每块带"前文已写过的话题"
      避免重复，单块失败跳过不致命。每段逐字稿都被模型实际读到，杜绝盲总结。
    """
    n = len(transcript)

    # 全局大纲（结构引导；失败不影响主流程）
    topics = generate_outline(transcript)
    topics_block = ("参考大纲（按需调整，不必逐条对应）：\n"
                    + "\n".join(f"- {t}" for t in topics)) if topics else ""

    # ---- 小转录：一次性全文 ----
    if n < 18000:
        user_msg = (
            f"以下是一个播客的完整逐字稿，请生成一份完整的中文讲书稿。\n\n"
            + (f"{topics_block}\n\n" if topics_block else "")
            + f"【逐字稿】\n{transcript}\n\n"
            f"请按核心话题组织讲稿，每个话题用 ## 标题开头。"
            f"第一个话题直接切入内容，不要全局开场白。"
            f"充分展开每个话题的论证过程、背景和推导链条。"
            f"目标篇幅约 {target} 字。\n\n"
            f"⚠ 重要：直接输出讲稿正文。不要写任务分析、不要写计划、不要写思考过程。"
        )
        briefing = call_deepseek(system_prompt, user_msg, max_tokens=8000)
        print(f"{progress_prefix} 一次性生成完成，{len(briefing)} 字", flush=True)
        return briefing

    # ---- 大转录：分块续写 ----
    chunks = chunk_transcript(transcript)
    total = len(chunks)
    per_target = max(1500, target // total)
    print(f"{progress_prefix} 分块生成：{total} 块，每块目标 ~{per_target} 字",
          flush=True)

    parts = []
    covered = []
    for i, chunk in enumerate(chunks, 1):
        if i == 1:
            user_msg = (
                f"这是播客逐字稿的第 1/{total} 段（开头部分）。请撰写中文讲稿的开头部分。\n\n"
                + (f"{topics_block}\n\n" if topics_block else "")
                + f"【本段逐字稿】\n{chunk}\n\n"
                f"按核心话题组织，每个话题用 ## 标题开头。第一个话题直接切入内容，不要全局开场白。"
                f"只撰写本段逐字稿涵盖的内容，目标 ~{per_target} 字。\n\n"
                f"⚠ 直接输出讲稿正文，不要任务分析/计划/思考过程。"
            )
        else:
            covered_str = "、".join(covered[-12:]) if covered else "（无）"
            user_msg = (
                f"这是播客逐字稿的第 {i}/{total} 段。请继续撰写讲稿，延续前文的话题和风格。\n\n"
                + (f"{topics_block}\n\n" if topics_block else "")
                + f"【本段逐字稿】\n{chunk}\n\n"
                f"前文已写过的话题（不要重复，仅在有必要补充时简短带过）：{covered_str}\n"
                f"只撰写本段逐字稿带来的新内容。如果本段全是跑题或重复，"
                f"就只输出一行：[本段无新增有效内容]。\n"
                f"目标 ~{per_target} 字。\n\n"
                f"⚠ 直接输出讲稿正文，不要任务分析/计划/思考过程。"
            )
        try:
            section = call_deepseek(system_prompt, user_msg, max_tokens=8000)
        except Exception as e:
            print(f"{progress_prefix} 块 {i}/{total} 失败，跳过: {e}", flush=True)
            continue
        if "[本段无新增有效内容]" in section:
            print(f"{progress_prefix} 块 {i}/{total} 跳过（无新增内容）", flush=True)
            continue
        parts.append(section.strip())
        covered.extend(_extract_headings(section))
        print(f"{progress_prefix} 块 {i}/{total} 完成，累计 {sum(len(p) for p in parts)} 字",
              flush=True)

    briefing = "\n\n".join(parts)
    print(f"{progress_prefix} 全部分块完成，共 {len(briefing)} 字", flush=True)
    return briefing

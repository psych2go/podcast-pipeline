"""
播客处理流水线 v4 · 抓取 + TTS（讲稿生成由 Claude Code 终端直接完成）

架构:
  抓取转录 (fetcher.py, 自动)
    → Claude 写讲稿 (交互, 参考 scripts/讲稿提示词.md)
    → TTS 出音频 (tts.py, 自动)

本脚本只负责"抓取"和"TTS"两端的自动化；讲稿生成由 Claude Code 在对话中
直接完成，不再调用任何外部 LLM API。

用法:
  # 1) 抓取转录（URL / mp3 / 本地转录文件），写好 原始转录.txt 后停下
  python scripts/process.py "URL" --name "播客名"
  python scripts/process.py "音频.mp3" --name "播客名"           # 默认 Whisper + 说话人分离
  python scripts/process.py "音频.mp3" --name "播客名" --no-diarize  # 跳过说话人分离
  python scripts/process.py "音频.mp3" --name "播客名" --asr-model medium  # 更快模型
  python scripts/process.py --transcript "转录.txt" --name "播客名"
  python scripts/process.py "URL" --name "播客名" --fetch-only

  # 2) （Claude 读 原始转录.txt，按 scripts/讲稿提示词.md 写 讲书稿.md）

  # 3) 对已有讲稿跑 TTS 出音频
  python scripts/process.py --name "播客名" --tts-only
  #   或直接：python scripts/tts.py "content/播客名/讲书稿.md"
"""
import sys
from pathlib import Path

# Ensure scripts/ is in sys.path for direct execution
_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import argparse
import os
import re

from config import BASE_DIR
from fetcher import (
    fetch_transcript_from_url,
    transcribe_mp3,
    transcribe,
    load_transcript_from_file,
    extract_title_from_url,
    make_initial_prompt,
)
from tts import run_tts
from html_gen import md_to_html
from validator import validate_and_fix, structure_report


# 讲稿文件名候选（新统一用 讲书稿.md；简报.md 仅向后兼容旧产物）
BRIEFING_CANDIDATES = ("讲书稿.md", "简报.md")
HTML_CANDIDATES = ("讲书稿.html",)


def sanitize_title(name):
    """清理文件夹/音频文件名：去掉文件系统与 shell 通配符不安全的字符，折叠空白。

    清理 \\ / : * ? " < > | [ ]（跨平台 + 避免 glob 误匹配），保留逗号、空格、中文等。
    例："Can the AI Industry Regulate Itself? Stripe..." → "Can the AI Industry Regulate Itself Stripe..."
    """
    name = re.sub(r'[\\/:*?"<>|\[\]$`]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(".")  # Windows 不允许文件名以点结尾
    return name or "untitled"


def detect_briefing(folder):
    """定位讲稿文件（兼容 {文件夹名} - 讲书稿.md 与裸 讲书稿.md 命名）。"""
    name = folder.name
    for cand in (f"{name} - 讲书稿.md", "讲书稿.md",
                 f"{name} - 简报.md", "简报.md"):
        p = folder / cand
        if p.exists():
            return cand, p
    hits = list(folder.glob("*讲书稿.md")) or list(folder.glob("*简报.md"))
    if hits:
        return hits[0].name, hits[0]
    return None, None


def fetch_transcript(source, folder, name, asr_model, initial_prompt=None,
                     hotwords=None, quality="balanced", engine="whisper",
                     lm_path=None, diarize_audio=True,
                     min_speakers=None, max_speakers=None):
    """抓取/读取转录，写入 原始转录.txt 和 来源.md。返回是否成功。"""
    transcript_path = folder / "原始转录.txt"
    transcript = None

    if source:
        if source.startswith("http"):
            transcript = fetch_transcript_from_url(source)
        elif os.path.isfile(source):
            if source.endswith(".mp3"):
                # 自动生成 initial_prompt（从标题），用户显式传的优先级更高
                auto_prompt = make_initial_prompt(name)
                effective_prompt = initial_prompt or auto_prompt

                # 统一 transcribe() 入口：whisper / parakeet 都支持 diarize
                transcript = transcribe(
                    source, engine=engine, quality=quality,
                    asr_model=asr_model,
                    initial_prompt=effective_prompt, hotwords=hotwords,
                    lm_path=lm_path, diarize_audio=diarize_audio,
                    min_speakers=min_speakers, max_speakers=max_speakers)
                if effective_prompt and not initial_prompt:
                    print(f"  [ASR] 自动 initial_prompt: {effective_prompt[:120]}", flush=True)
            else:
                transcript = load_transcript_from_file(source)
        else:
            print(f"[错误] 无法识别输入源: {source}", flush=True)

    # 回退：读已有转录
    if not transcript and transcript_path.exists():
        transcript = transcript_path.read_text(encoding="utf-8")

    if not transcript or len(transcript) < 200:
        print("[错误] 无法获取转录文本", flush=True)
        return False

    transcript_path.write_text(transcript, encoding="utf-8")
    print(f"[转录] {len(transcript)} 字符 → {transcript_path.name}", flush=True)

    # 来源信息（首次创建）
    source_path = folder / "来源.md"
    if not source_path.exists() and source:
        from datetime import date
        today = date.today().isoformat()
        lines = ["# 来源信息", "", "## 原始播客", f"- 标题：{name}"]
        if source.startswith("http"):
            lines += [f"- 链接：{source}", f"- 转录来源：{source}"]
        else:
            lines.append(f"- 输入文件：{source}")
            # 音频转写路径：标记本地 ASR，纠错关口据此判断是否强制纠错
            if source.lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg")):
                lines.append("- 转录方式：本地 ASR（Whisper）")
        lines += ["", "## 处理信息",
                  f"- 处理日期：{today}", f"- pipeline 版本：v4"]
        source_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[来源] 已创建 {source_path.name}", flush=True)

    return True


def _run_structure_check(text):
    """跑结构体检并打印报告（只报告，不自动修改内容）。"""
    warns = structure_report(text)
    if warns:
        print("\n[结构体检] 发现以下问题（供参考，不会自动修改）：")
        for w in warns:
            print(f"  ⚠ {w}")
        print()
    else:
        print("[结构体检] 通过", flush=True)


def run_tts_step(folder, name, briefing_file, tts_speed, force_tts, read_titles):
    # 校验并自动修复讲稿
    briefing_path = folder / briefing_file
    if briefing_path.exists():
        text = briefing_path.read_text(encoding="utf-8")
        _run_structure_check(text)
        fixed, issues = validate_and_fix(text)
        if issues:
            briefing_path.write_text(fixed, encoding="utf-8")
            if len(issues) > 0:
                print(f"[校验] 讲稿已自动修复 {len(issues)} 个问题", flush=True)

    print(f"[TTS] 开始生成音频（讲稿: {briefing_file}）...", flush=True)
    result = run_tts(str(folder), briefing_file, name,
                     speed=tts_speed, fresh=force_tts, read_titles=read_titles)
    print(f"[TTS] {result}", flush=True)
    print(f"\n[完成] 音频: {folder / f'{name}.mp3'}", flush=True)


def _display_title(name):
    """从 site/site.json 查该期的精修标题（带标点）；查不到回退文件夹名。

    保证 content.html 的 hero 标题与首页卡片一致（site.json 是标题单一数据源）。
    """
    try:
        import json
        site_json = Path(BASE_DIR).parent / "site" / "site.json"
        eps = json.loads(site_json.read_text(encoding="utf-8"))
        for e in eps:
            if e.get("folder") == name:
                return e.get("title") or name
    except Exception:
        pass
    return name


def run_html_step(folder, name, briefing_file):
    """生成 HTML 阅读页面（前置校验讲稿）。"""
    md_path = folder / briefing_file
    if not md_path.exists():
        print(f"[HTML] 跳过：找不到 {md_path}", flush=True)
        return False
    # 校验并自动修复讲稿
    text = md_path.read_text(encoding="utf-8")
    _run_structure_check(text)
    fixed, issues = validate_and_fix(text)
    if issues:
        md_path.write_text(fixed, encoding="utf-8")
    # 输出统一为 "{name} - content.html"（与 catalog.py / 部署约定一致）
    out_path = folder / f"{name} - content.html"
    result = md_to_html(str(md_path), output_path=str(out_path),
                        podcast_title=_display_title(name))
    return result is not None


def _warn_if_asr_needs_correction(folder):
    """本地 ASR 转录且无纠错文件时，提醒先纠错再写稿（纠错关口）。"""
    source_path = folder / "来源.md"
    if not source_path.exists():
        return
    src_text = source_path.read_text(encoding="utf-8")
    if "本地 ASR" in src_text and not (folder / "转录_纠错.txt").exists():
        print(
            "\n[关口提醒] 此期为本地 ASR 转录——专有名词错听是 ASR 最大弱点。\n"
            "  请先按 scripts/纠错提示词.md 产出 转录_纠错.txt，再写 讲书稿.md。\n",
            flush=True,
        )


def process(source, name, asr_model="large-v3", tts_speed=1.0,
            force_tts=False, read_titles=True, fetch_only=False, tts_only=False,
            html_only=False, no_html=False, quality="balanced",
            initial_prompt=None, hotwords=None, engine="whisper",
            lm_path=None, diarize_audio=True,
            min_speakers=None, max_speakers=None):
    """抓取转录 / 跑 TTS。讲稿生成由 Claude 在对话中完成。"""

    folder = BASE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)

    # ---- 仅 HTML 模式：从已有讲稿生成 HTML，跳过其他一切 ----
    if html_only:
        briefing_file, briefing_path = detect_briefing(folder)
        if not briefing_path:
            print(f"[错误] {folder} 下没有讲稿（讲书稿.md / 简报.md）。", flush=True)
            return
        run_html_step(folder, name, briefing_file)
        return

    # ---- 仅 TTS 模式：跳过抓取，对已有讲稿出音频 ----
    if tts_only:
        briefing_file, briefing_path = detect_briefing(folder)
        if not briefing_path:
            print(f"[错误] {folder} 下没有讲稿（讲书稿.md / 简报.md）。"
                  f"先让 Claude 读取 原始转录.txt 生成 讲书稿.md。", flush=True)
            return
        run_tts_step(folder, name, briefing_file, tts_speed, force_tts, read_titles)
        if not no_html:
            run_html_step(folder, name, briefing_file)
        return

    # ---- 抓取转录 ----
    if not source:
        print("[错误] 需要 source（URL / mp3 / 转录文件）或加 --tts-only", flush=True)
        return
    if not fetch_transcript(source, folder, name, asr_model, initial_prompt, hotwords,
                             quality=quality, engine=engine, lm_path=lm_path,
                             diarize_audio=diarize_audio,
                             min_speakers=min_speakers, max_speakers=max_speakers):
        return
    _warn_if_asr_needs_correction(folder)

    # ---- 抓取后：没有讲稿就停下，等 Claude 生成 ----
    briefing_file, briefing_path = detect_briefing(folder)
    if fetch_only or not briefing_path:
        print(
            "\n[下一步] 转录已就绪。请让 Claude 读取\n"
            f"  {folder / '原始转录.txt'}\n"
            "并按 scripts/讲稿提示词.md 生成 讲书稿.md，完成后运行：\n"
            f"  python scripts/process.py --name \"{name}\" --tts-only",
            flush=True,
        )
        return

    # 讲稿已存在 → 直接出音频
    run_tts_step(folder, name, briefing_file, tts_speed, force_tts, read_titles)
    if not no_html:
        run_html_step(folder, name, briefing_file)


# ===================================================================
#  命令行入口
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description="播客处理流水线 v4（抓取 + TTS；讲稿由 Claude 直接生成）"
    )
    parser.add_argument("source", nargs="?",
                        help="URL / mp3 / 转录文件路径（--tts-only 时可省略）")
    parser.add_argument("--name", default=None, help="播客文件夹名称（不给则从 URL 自动提取标题；命名即原始标题）")
    parser.add_argument("--transcript", help="直接指定转录文件路径")
    parser.add_argument("--fetch-only", action="store_true",
                        help="只抓转录，不跑 TTS（生成交给 Claude）")
    parser.add_argument("--tts-only", action="store_true",
                        help="跳过抓取，对已有讲稿直接跑 TTS")
    parser.add_argument("--html-only", action="store_true",
                        help="跳过抓取和 TTS，仅从已有讲稿生成 HTML")
    parser.add_argument("--no-html", action="store_true",
                        help="TTS 后不自动生成 HTML")
    parser.add_argument("--asr-model", default=None,
                        help="Whisper 模型大小（默认 large-v3-turbo；可选 large-v3/distil-large-v3/medium）")
    parser.add_argument("--asr-quality", default="balanced",
                        choices=["fast", "balanced", "max"],
                        help="ASR 质量预设: fast(快速/medium) / balanced(默认/large-v3) / max(最高精度/large-v3+全调优)")
    parser.add_argument("--asr-engine", default="whisper",
                        choices=["whisper", "whisper-fast"],
                        help="ASR 引擎: whisper(默认,faster-whisper large-v3-turbo) / whisper-fast")
    parser.add_argument("--lm", default=None,
                        help="Parakeet n-gram LM 路径（.arpa/.bin），kenlm 可用时 shallow fusion")
    parser.add_argument("--diarize", action="store_true", default=True,
                        help="启用 pyannote 说话人分离（默认启用，需 HF_TOKEN）")
    parser.add_argument("--no-diarize", action="store_true",
                        help="跳过说话人分离（省时，无 SPEAKER 标签）")
    parser.add_argument("--min-speakers", type=int, default=None,
                        help="说话人分离：最少说话人数")
    parser.add_argument("--max-speakers", type=int, default=None,
                        help="说话人分离：最多说话人数")
    parser.add_argument("--tts-speed", type=float, default=1.0, help="TTS 语速（默认 1.0）")
    parser.add_argument("--force-tts", action="store_true",
                        help="TTS 清空旧音频重新生成（默认断点续传）")
    parser.add_argument("--no-tts-titles", action="store_true",
                        help="不在音频中朗读章节标题")
    parser.add_argument("--initial-prompt", default=None,
                        help="MP3 转录用：已知人名/公司/术语/背景，条件化 Whisper 减少专有名词错听")
    parser.add_argument("--hotwords", default=None,
                        help="MP3 转录用：逗号分隔的热词，加权特定词（如 'Naval,Gary Tan,Claude Code'）")

    args = parser.parse_args()

    source = args.source
    if args.transcript:
        source = args.transcript
    if not source and not args.tts_only and not args.html_only:
        parser.print_help()
        sys.exit(1)

    # 名字解析：手动指定 > 从 URL 自动提取标题；最后统一清理 unsafe 字符
    name = args.name
    if not name and source and source.startswith("http"):
        name = extract_title_from_url(source)
    if not name:
        print("[错误] 缺少 --name，且无法从 source 自动提取标题", flush=True)
        sys.exit(1)
    name = sanitize_title(name)
    print(f"[命名] {name}", flush=True)

    # 说话人分离：--diarize 默认启用，--no-diarize 可覆盖
    _diarize = args.diarize and not args.no_diarize
    process(source, name, args.asr_model, args.tts_speed,
            args.force_tts, not args.no_tts_titles, args.fetch_only, args.tts_only,
            args.html_only, args.no_html, quality=args.asr_quality,
            initial_prompt=args.initial_prompt, hotwords=args.hotwords,
            engine=args.asr_engine, lm_path=args.lm,
            diarize_audio=_diarize,
            min_speakers=args.min_speakers, max_speakers=args.max_speakers)


if __name__ == "__main__":
    main()

"""
播客处理流水线 v7 · 抓取、质量门、TTS 与 HTML（讲稿由 Claude Code 生成）

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
import hashlib
import json
import os
import re
from contextlib import nullcontext

from config import BASE_DIR
from fetcher import (
    fetch_transcript_from_url,
    transcribe_mp3,
    transcribe,
    load_transcript_from_file,
    extract_title_from_url,
    make_initial_prompt,
    detect_source_warnings,
    apply_content_policy,
    chunk_plain_transcript,
)
from tts import run_tts
from html_gen import md_to_html
from run_report import RunReport
from tts import build_tts_plan
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


def _is_audio_file(path):
    return str(path).lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"))


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _stage(run_report, name, metrics=None):
    if run_report is None:
        return nullcontext(None)
    return run_report.stage(name, metrics)


def _quality_metrics(folder):
    path = Path(folder) / "quality_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "passed": bool(report.get("passed")),
        "error_count": len(report.get("errors", [])),
        "warning_count": len(report.get("warnings", [])),
        "claim_coverage": report.get("coverage", {}).get("claim_coverage"),
        "notes_claim_coverage": report.get(
            "coverage", {}).get("notes_claim_coverage"),
    }


def _tts_metrics(folder, briefing_file):
    folder = Path(folder)
    path = folder / "tts_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    config = manifest.get("config", {})
    plan = build_tts_plan(
        folder,
        briefing_file,
        speed=config.get("speed", 1.0),
        read_titles=config.get("read_titles", True),
    )
    plan_by_name = {item["filename"]: item for item in plan}
    sections = manifest.get("sections", [])
    synthesized = [
        section for section in sections
        if section.get("status") == "complete" and not section.get("cached")
    ]
    metrics = {
        "completed": bool(manifest.get("completed")),
        "expected_sections": manifest.get("expected_sections", 0),
        "completed_sections": manifest.get("completed_sections", 0),
        "cached_sections": sum(
            section.get("status") == "complete" and section.get("cached")
            for section in sections
        ),
        "synthesized_sections": len(synthesized),
        "synthesized_characters": sum(
            len(plan_by_name.get(section.get("filename"), {}).get("text", ""))
            for section in synthesized
        ),
        "failed_sections": manifest.get("failed_sections", []),
        "final_size_bytes": manifest.get("final", {}).get("size"),
    }
    metrics.update(manifest.get("usage", {}))
    return metrics


def _source_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _upsert_source_field(text, label, value):
    if not value:
        return text
    pattern = rf"^- {re.escape(label)}：.*$"
    replacement = f"- {label}：{value}"
    if re.search(pattern, text, re.MULTILINE):
        return re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    return text.rstrip() + "\n" + replacement + "\n"


def fetch_transcript(source, folder, name, asr_model, initial_prompt=None,
                     hotwords=None, quality="balanced", engine="whisper",
                     lm_path=None, diarize_audio=True,
                     min_speakers=None, max_speakers=None,
                     content_policy="faithful", display_title=None):
    """抓取/读取转录，写入纯文本和可审计的 transcript.raw.json。"""
    transcript_path = folder / "原始转录.txt"
    metadata_path = folder / "transcript.raw.json"
    transcript = None
    metadata = None
    source_kind = "existing"

    if source:
        if source.startswith("http"):
            source_kind = "web_transcript"
            fetched = fetch_transcript_from_url(source, return_metadata=True)
            if isinstance(fetched, dict):
                transcript = fetched["text"]
                metadata = {
                    "source": source,
                    "source_kind": source_kind,
                    "segments": fetched.get("segments", []),
                    "meta": {
                        **fetched.get("meta", {}),
                        "source_warnings": detect_source_warnings(transcript),
                    },
                }
            else:
                transcript = fetched
                if transcript:
                    metadata = {
                        "source": source,
                        "source_kind": source_kind,
                        "segments": [
                            {"start": None, "end": None, "text": chunk}
                            for chunk in chunk_plain_transcript(transcript)
                        ],
                        "meta": {"timestamped": False, "source_warnings": detect_source_warnings(transcript)},
                    }
        elif os.path.isfile(source):
            if _is_audio_file(source):
                source_kind = "local_asr"
                auto_prompt = make_initial_prompt(name)
                effective_prompt = initial_prompt or auto_prompt
                result = transcribe(
                    source, engine=engine, quality=quality,
                    asr_model=asr_model,
                    initial_prompt=effective_prompt, hotwords=hotwords,
                    lm_path=lm_path, diarize_audio=diarize_audio,
                    min_speakers=min_speakers, max_speakers=max_speakers,
                    return_metadata=True,
                )
                transcript = result["text"]
                metadata = {
                    "source": os.path.abspath(source),
                    "source_kind": source_kind,
                    "source_sha256": _source_sha256(source),
                    **result,
                }
                if effective_prompt:
                    metadata["meta"]["initial_prompt"] = effective_prompt
                if hotwords:
                    metadata["meta"]["hotwords"] = hotwords
                if effective_prompt and not initial_prompt:
                    print(f"  [ASR] 自动 initial_prompt: {effective_prompt[:120]}", flush=True)
            else:
                source_kind = "local_transcript"
                transcript = load_transcript_from_file(source)
                metadata = {
                    "source": os.path.abspath(source),
                    "source_kind": source_kind,
                    "segments": [
                        {"start": None, "end": None, "text": chunk}
                        for chunk in chunk_plain_transcript(transcript)
                    ],
                    "meta": {"timestamped": False, "source_warnings": detect_source_warnings(transcript)},
                }
        else:
            print(f"[错误] 无法识别输入源: {source}", flush=True)

    # 回退：读已有转录。不要因为已有文本而覆盖已有结构化结果。
    if not transcript and transcript_path.exists():
        transcript = transcript_path.read_text(encoding="utf-8")
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = None

    if not transcript or len(transcript) < 200:
        print("[错误] 无法获取转录文本", flush=True)
        return False

    if source_kind in {"web_transcript", "local_transcript"}:
        transcript = apply_content_policy(transcript, content_policy)
        if metadata:
            metadata.setdefault("meta", {})["content_policy"] = content_policy

    transcript_path.write_text(transcript, encoding="utf-8")
    if metadata:
        for index, segment in enumerate(metadata.get("segments", []), start=1):
            segment.setdefault("id", f"S{index:04d}")
        metadata.setdefault("meta", {})["transcript_chars"] = len(transcript)
        metadata["meta"]["transcript_file"] = transcript_path.name
        _write_json(metadata_path, metadata)
    print(f"[转录] {len(transcript)} 字符 → {transcript_path.name}", flush=True)
    if metadata:
        print(f"[转录] 结构化结果 → {metadata_path.name}", flush=True)

    # 来源信息是人类可读镜像；episode.json 是结构化单一数据源。
    source_path = folder / "来源.md"
    if not source_path.exists() and source:
        from datetime import date
        today = date.today().isoformat()
        lines = [
            "# 来源信息", "", "## 原始播客",
            f"- 标题：{display_title or name}",
        ]
        if source.startswith("http"):
            lines += [f"- 链接：{source}", f"- 转录来源：{source}"]
            lines.append("- 转录方式：网页转录抓取")
        else:
            lines.append(f"- 输入文件：{source}")
            if _is_audio_file(source):
                lines.append("- 转录方式：本地 ASR（Whisper）")
        lines += ["", "## 处理信息", f"- 处理日期：{today}",
                  "- pipeline 版本：v7", f"- ASR 质量：{quality}"]
        if metadata and metadata.get("meta", {}).get("diarization"):
            lines.append("- 说话人分离：已启用")
        source_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[来源] 已创建 {source_path.name}", flush=True)
    elif source_path.exists() and source:
        from datetime import date
        text = source_path.read_text(encoding="utf-8")
        text = _upsert_source_field(
            text, "标题", display_title or name)
        if source.startswith("http"):
            text = _upsert_source_field(text, "链接", source)
            text = _upsert_source_field(text, "转录来源", source)
            text = _upsert_source_field(text, "转录方式", "网页转录抓取")
        else:
            text = _upsert_source_field(text, "输入文件", source)
            if _is_audio_file(source):
                text = _upsert_source_field(
                    text, "转录方式", "本地 ASR（Whisper）")
        text = _upsert_source_field(
            text, "处理日期", date.today().isoformat())
        text = _upsert_source_field(text, "pipeline 版本", "v7")
        text = _upsert_source_field(text, "ASR 质量", quality)
        source_path.write_text(text, encoding="utf-8")

    from episode import ensure_episode
    ensure_episode(
        folder,
        display_title=display_title or name,
        source_url=source if source and source.startswith("http") else "",
        source_kind=source_kind,
        extractor=(
            metadata.get("meta", {}).get("extractor", "")
            if metadata else ""
        ),
        quality_mode="strict",
    )

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


def _run_quality_gate(folder, auto_ai_review=True, allow_legacy=False):
    """如果存在内容台账，则在 TTS 前阻断未通过的完整性检查。"""
    content_map = folder / "content_map.json"
    if not content_map.exists() and allow_legacy:
        print(
            "[质量门][兼容] 未找到 content_map.json；"
            "已通过显式参数允许旧期仅做结构校验",
            flush=True,
        )
        return True
    from quality_report import build_quality_report
    report = build_quality_report(folder, strict=True)
    out = folder / "quality_report.json"
    _write_json(out, report)
    review_only_prefixes = (
        "来源质量未通过自动关口:",
        "内容审查状态未通过:",
        "缺少 ai_review.json",
        "AI ",
    )
    review_missing_or_stale = any(
        error.startswith("缺少 ai_review.json")
        or error.startswith("AI 审查已过期")
        for error in report.get("errors", [])
    )
    can_auto_review = review_missing_or_stale and all(
        error.startswith(review_only_prefixes)
        for error in report["errors"]
    )
    if not report.get("passed", False) and auto_ai_review and can_auto_review:
        print("[质量门] AI 审查缺失或过期，自动运行 Claude opus/max...", flush=True)
        try:
            from ai_review import review_episode
            review_episode(
                folder,
                model=os.environ.get("AI_REVIEW_MODEL", "opus"),
                effort=os.environ.get("AI_REVIEW_EFFORT", "max"),
            )
        except Exception as exc:
            print(f"[质量门][阻断] 自动 AI 审查执行失败: {exc}", flush=True)
            return False
        report = build_quality_report(folder)
        _write_json(out, report)
    for warning in report.get("warnings", []):
        print(f"[质量门][警告] {warning}", flush=True)
    if not report.get("passed", False):
        for error in report.get("errors", []):
            print(f"[质量门][阻断] {error}", flush=True)
        print(f"[质量门] 未通过，已写入 {out.name}，不会进入 TTS", flush=True)
        return False
    print("[质量门] 内容完整性检查通过", flush=True)
    return True


def run_tts_step(folder, name, briefing_file, tts_speed, force_tts, read_titles,
                 auto_ai_review=True, allow_legacy=False, run_report=None):
    # 所有可能修改讲稿的操作必须发生在 AI 审查/哈希质量门之前。
    # 否则会出现“审查通过后讲稿又被修改，仍继续生成音频”的 TOCTOU 问题。
    with _stage(run_report, "prepare_briefing") as stage:
        briefing_path = folder / briefing_file
        if briefing_path.exists():
            text = briefing_path.read_text(encoding="utf-8")
            _run_structure_check(text)
            fixed, issues = validate_and_fix(text)
            if stage is not None:
                stage.metrics.update({
                    "validator_issue_count": len(issues),
                    "briefing_modified": fixed != text,
                })
            if fixed != text:
                briefing_path.write_text(fixed, encoding="utf-8")
                print(
                    f"[校验] 讲稿已自动修复；校验共发现 {len(issues)} 个问题；"
                    "现有 AI 审查将因哈希变化而失效",
                    flush=True,
                )

    with _stage(run_report, "quality_gate") as stage:
        quality_ok = _run_quality_gate(
            folder,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy)
        if stage is not None:
            stage.metrics.update(_quality_metrics(folder))
            if not quality_ok:
                stage.fail("quality gate failed")
        if not quality_ok:
            return False

    with _stage(run_report, "tts") as stage:
        print(f"[TTS] 开始生成音频（讲稿: {briefing_file}）...", flush=True)
        result = run_tts(
            str(folder), briefing_file, name,
            speed=tts_speed, fresh=force_tts, read_titles=read_titles)
        print(f"[TTS] {result}", flush=True)
        if stage is not None:
            stage.metrics.update(_tts_metrics(folder, briefing_file))
            if not result.ok:
                stage.fail(result.summary)
        if not result.ok:
            print("[TTS][阻断] 存在失败章节，最终音频未更新", flush=True)
            return False
    print(f"\n[完成] 音频: {folder / f'{name}.mp3'}", flush=True)
    return True


def _display_title(name):
    """从 episode.json 读取展示标题；旧期自动从现有元数据回退。"""
    try:
        from episode import display_title
        return display_title(BASE_DIR / name)
    except Exception:
        return name


def run_html_step(
        folder, name, briefing_file, auto_ai_review=True,
        allow_legacy=False, run_report=None):
    """生成 HTML 阅读页面；任何自动修复后都必须重新通过质量门。"""
    md_path = folder / briefing_file
    if not md_path.exists():
        print(f"[HTML] 跳过：找不到 {md_path}", flush=True)
        return False
    with _stage(run_report, "html_quality_gate") as stage:
        text = md_path.read_text(encoding="utf-8")
        _run_structure_check(text)
        fixed, issues = validate_and_fix(text)
        if stage is not None:
            stage.metrics.update({
                "validator_issue_count": len(issues),
                "briefing_modified": fixed != text,
            })
        if fixed != text:
            md_path.write_text(fixed, encoding="utf-8")
        quality_ok = _run_quality_gate(
            folder,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy)
        if stage is not None:
            stage.metrics.update(_quality_metrics(folder))
            if not quality_ok:
                stage.fail("quality gate failed")
        if not quality_ok:
            return False
    with _stage(run_report, "html") as stage:
        out_path = folder / f"{name} - content.html"
        result = md_to_html(
            str(md_path), output_path=str(out_path),
            podcast_title=_display_title(name))
        if stage is not None:
            stage.metrics.update({
                "output": out_path.name,
                "size_bytes": out_path.stat().st_size
                if out_path.exists() else 0,
            })
            if result is None:
                stage.fail("HTML generation failed")
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


def _process_impl(
        source, name, folder, run_report, asr_model=None, tts_speed=1.0,
        force_tts=False, read_titles=True, fetch_only=False, tts_only=False,
        html_only=False, no_html=False, quality="balanced",
        initial_prompt=None, hotwords=None, engine="whisper",
        lm_path=None, diarize_audio=True,
        min_speakers=None, max_speakers=None,
        content_policy="faithful", auto_ai_review=True,
        allow_legacy=False, display_title=None):

    # ---- 仅 HTML 模式：从已有讲稿生成 HTML，跳过其他一切 ----
    if html_only:
        briefing_file, briefing_path = detect_briefing(folder)
        if not briefing_path:
            print(f"[错误] {folder} 下没有讲稿（讲书稿.md / 简报.md）。", flush=True)
            return False
        return run_html_step(
            folder, name, briefing_file,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy,
            run_report=run_report)

    # ---- 仅 TTS 模式：跳过抓取，对已有讲稿出音频 ----
    if tts_only:
        briefing_file, briefing_path = detect_briefing(folder)
        if not briefing_path:
            print(f"[错误] {folder} 下没有讲稿（讲书稿.md / 简报.md）。"
                  f"先让 Claude 读取 原始转录.txt 生成 讲书稿.md。", flush=True)
            return False
        if not run_tts_step(
                folder, name, briefing_file, tts_speed, force_tts, read_titles,
                auto_ai_review=auto_ai_review,
                allow_legacy=allow_legacy,
                run_report=run_report):
            return False
        if not no_html:
            return run_html_step(
                folder, name, briefing_file,
                auto_ai_review=auto_ai_review,
                allow_legacy=allow_legacy,
                run_report=run_report)
        return True

    # ---- 抓取转录 ----
    if not source:
        print("[错误] 需要 source（URL / mp3 / 转录文件）或加 --tts-only", flush=True)
        return False
    with _stage(run_report, "fetch") as stage:
        fetched = fetch_transcript(
            source, folder, name, asr_model, initial_prompt, hotwords,
            quality=quality, engine=engine, lm_path=lm_path,
            diarize_audio=diarize_audio,
            min_speakers=min_speakers, max_speakers=max_speakers,
            content_policy=content_policy,
            display_title=display_title)
        raw_path = folder / "transcript.raw.json"
        if stage is not None and raw_path.exists():
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            stage.metrics.update({
                "source_kind": raw.get("source_kind"),
                "segment_count": len(raw.get("segments", [])),
                "transcript_chars": raw.get(
                    "meta", {}).get("transcript_chars"),
            })
        if stage is not None and not fetched:
            stage.fail("transcript fetch failed")
        if not fetched:
            return False
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
        return True

    # 讲稿已存在 → 直接出音频
    if not run_tts_step(
            folder, name, briefing_file, tts_speed, force_tts, read_titles,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy,
            run_report=run_report):
        return False
    if not no_html:
        return run_html_step(
            folder, name, briefing_file,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy,
            run_report=run_report)
    return True


def process(source, name, asr_model=None, tts_speed=1.0,
            force_tts=False, read_titles=True, fetch_only=False, tts_only=False,
            html_only=False, no_html=False, quality="balanced",
            initial_prompt=None, hotwords=None, engine="whisper",
            lm_path=None, diarize_audio=True,
            min_speakers=None, max_speakers=None,
            content_policy="faithful", auto_ai_review=True,
            allow_legacy=False, display_title=None):
    """抓取转录 / 跑 TTS，并将每次执行追加到 run_report.json。"""
    folder = BASE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    mode = (
        "html-only" if html_only
        else "tts-only" if tts_only
        else "fetch-only" if fetch_only
        else "full"
    )
    report = RunReport(folder, "process", {
        "mode": mode,
        "source": source if source and source.startswith("http") else (
            str(source) if source else ""),
        "asr_quality": quality,
        "asr_model": asr_model,
        "tts_speed": tts_speed,
    })
    try:
        ok = _process_impl(
            source, name, folder, report, asr_model, tts_speed,
            force_tts, read_titles, fetch_only, tts_only,
            html_only, no_html, quality, initial_prompt, hotwords, engine,
            lm_path, diarize_audio, min_speakers, max_speakers,
            content_policy, auto_ai_review, allow_legacy, display_title)
    except Exception as exc:
        report.finish(False, exc)
        raise
    report.finish(ok, None if ok else "pipeline returned failure")
    return ok


# ===================================================================
#  命令行入口
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description="播客处理流水线 v7（抓取、严格质量门、TTS 与 HTML）"
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
                        help="Whisper 模型大小（不传则由 ASR 质量预设决定；可选 large-v3/large-v3-turbo/medium）")
    parser.add_argument("--asr-quality", default="balanced",
                        choices=["fast", "balanced", "max"],
                        help="ASR 质量预设: fast(快速/medium) / balanced(默认/large-v3) / max(最高精度/large-v3+全调优)")
    parser.add_argument("--asr-engine", default="whisper",
                        choices=["whisper", "whisper-fast"],
                        help="ASR 引擎: whisper(默认) / whisper-fast（快速预设）")
    parser.add_argument("--lm", default=None,
                        help="保留兼容性的语言模型参数（当前 Whisper 路径不使用）")
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

    parser.add_argument("--content-policy", default="faithful",
                        choices=["faithful", "no-ads", "summary-ready"],
                        help="网页/本地文本的编辑策略；默认 faithful 不静默删除内容")
    parser.add_argument("--skip-ai-review", action="store_true",
                        help="不自动调用 Claude 审查，只校验已有 ai_review.json")
    parser.add_argument(
        "--allow-legacy-quality",
        action="store_true",
        help="显式允许缺少 content_map.json 的旧期绕过完整质量门",
    )

    args = parser.parse_args()

    source = args.source
    if args.transcript:
        source = args.transcript
    if not source and not args.tts_only and not args.html_only:
        parser.print_help()
        sys.exit(1)

    # 名字解析：手动指定 > 从 URL 自动提取标题；最后统一清理 unsafe 字符
    raw_title = args.name
    if not raw_title and source and source.startswith("http"):
        raw_title = extract_title_from_url(source)
    if not raw_title:
        print("[错误] 缺少 --name，且无法从 source 自动提取标题", flush=True)
        sys.exit(1)
    name = sanitize_title(raw_title)
    print(f"[命名] {name}", flush=True)

    # 说话人分离：--diarize 默认启用，--no-diarize 可覆盖
    _diarize = args.diarize and not args.no_diarize
    ok = process(source, name, args.asr_model, args.tts_speed,
                 args.force_tts, not args.no_tts_titles, args.fetch_only, args.tts_only,
                 args.html_only, args.no_html, quality=args.asr_quality,
                 initial_prompt=args.initial_prompt, hotwords=args.hotwords,
                 engine=args.asr_engine, lm_path=args.lm,
                 diarize_audio=_diarize,
                 min_speakers=args.min_speakers, max_speakers=args.max_speakers,
                 content_policy=args.content_policy,
                 auto_ai_review=not args.skip_ai_review,
                 allow_legacy=args.allow_legacy_quality,
                 display_title=raw_title)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

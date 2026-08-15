"""
播客处理流水线 v8 · 抓取、subagent 内容编排、质量门、TTS 与 HTML

架构:
  抓取转录 (fetcher.py, 自动)
    → subagent 纠错、台账、笔记和讲稿
    → subagent claim evidence 和 AI review
    → TTS 出音频 (tts.py, 自动)

用法:
  # 1) 抓取并默认自动完成内容编排、TTS、HTML
  python scripts/process.py "URL" --name "播客名"
  python scripts/process.py "音频.mp3" --name "播客名"           # 默认 Whisper + 说话人分离
  python scripts/process.py "音频.mp3" --name "播客名" --no-diarize  # 跳过说话人分离
  python scripts/process.py "音频.mp3" --name "播客名" --asr-model medium  # 更快模型
  python scripts/process.py --transcript "转录.txt" --name "播客名"
  python scripts/process.py "URL" --name "播客名" --fetch-only

  # 2) 对已有讲稿跑 TTS 出音频
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
import json
import os
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from atomic_io import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    exclusive_file_lock,
)
from config import BASE_DIR, validate_for_stage
from evidence import build_provenance, original_audio_files
from asr_refinement import build_asr_context
from fetcher import (
    fetch_transcript_from_url,
    transcribe_mp3,
    transcribe,
    load_transcript_from_file,
    extract_title_from_url,
    detect_source_warnings,
    apply_content_policy,
    chunk_plain_transcript,
)
from tts import run_tts
from tts import load_tts_lexicon
from html_gen import md_to_html
try:
    from hashing import (
        sha256_file as _source_sha256, sha256_text as _text_sha256)
except ImportError:
    from scripts.hashing import (
        sha256_file as _source_sha256, sha256_text as _text_sha256)
from preflight import quality_gate as shared_quality_gate
from agent_pipeline import content_pipeline_needed, run_content_pipeline
from release import prepare_release
from run_report import RunReport
from tts import build_tts_plan
from content_finalizer import (
    ContentFinalizationError,
    finalize_content_artifacts,
    validate_tts_readiness,
)
from pipeline_metrics import quality_metrics as _quality_metrics
from validator import (
    normalize_briefing_artifacts,
    structure_report,
    validate_and_fix,
)


# 讲稿文件名候选（新统一用 讲书稿.md；简报.md 仅向后兼容旧产物）
BRIEFING_CANDIDATES = ("讲书稿.md", "简报.md")
HTML_CANDIDATES = ("讲书稿.html",)


@dataclass(frozen=True)
class EpisodeOptions:
    asr_model: str | None = None
    tts_speed: float = 1.0
    force_tts: bool = False
    read_titles: bool = True
    fetch_only: bool = False
    tts_only: bool = False
    html_only: bool = False
    no_html: bool = False
    quality: str = "balanced"
    initial_prompt: str | None = None
    hotwords: object | None = None
    engine: str = "whisper"
    lm_path: str | None = None
    diarize_audio: bool = True
    min_speakers: int | None = None
    max_speakers: int | None = None
    content_policy: str = "faithful"
    auto_ai_review: bool = True
    allow_legacy: bool = False
    display_title: str | None = None
    force_refetch: bool = False
    asr_language: str | None = "en"
    auto_content: bool = True
    adaptive_refinement: bool = True
    align_audio: bool = True

    @property
    def mode(self):
        if self.html_only:
            return "html-only"
        if self.tts_only:
            return "tts-only"
        if self.fetch_only:
            return "fetch-only"
        return "full"

    def run_metadata(self, source):
        return {
            "mode": self.mode,
            "source": source if source and str(source).startswith("http") else (
                str(source) if source else ""),
            "asr_quality": self.quality,
            "asr_model": self.asr_model,
            "asr_language": self.asr_language,
            "force_refetch": self.force_refetch,
            "adaptive_refinement": self.adaptive_refinement,
            "align_audio": self.align_audio,
            "tts_speed": self.tts_speed,
            "auto_content": self.auto_content,
        }


def sanitize_title(name):
    """清理文件夹/音频文件名：去掉文件系统与 shell 通配符不安全的字符，折叠空白。

    清理 \\ / : * ? " < > | [ ]（跨平台 + 避免 glob 误匹配），保留逗号、空格、中文等。
    例："Can the AI Industry Regulate Itself? Stripe..." → "Can the AI Industry Regulate Itself Stripe..."
    """
    name = re.sub(r'[\\/:*?"<>|\[\]$`]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(".")  # Windows 不允许文件名以点结尾
    encoded = name.encode("utf-8")
    if len(encoded) > 180:
        name = encoded[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return name or "untitled"


def detect_briefing(folder):
    """定位讲稿文件（兼容 {文件夹名} - 讲书稿.md 与裸 讲书稿.md 命名）。"""
    name = folder.name
    for cand in ("讲书稿.md", f"{name} - 讲书稿.md",
                 "简报.md", f"{name} - 简报.md"):
        p = folder / cand
        if p.exists():
            return cand, p
    hits = sorted(folder.glob("*讲书稿.md")) or sorted(
        folder.glob("*简报.md"))
    if hits:
        return hits[0].name, hits[0]
    return None, None


def _is_audio_file(path):
    return str(path).lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"))


def _write_json(path, payload):
    atomic_write_json(path, payload)


def _stage(run_report, name, metrics=None):
    if run_report is None:
        return nullcontext(None)
    return run_report.stage(name, metrics)


def _tts_metrics(folder, briefing_file):
    folder = Path(folder)
    path = folder / "tts_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    config = manifest.get("config", {})
    try:
        plan = build_tts_plan(
            folder,
            briefing_file,
            speed=config.get("speed", 1.0),
            read_titles=config.get("read_titles", True),
        )
    except Exception:
        return {}
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


def _segment_text_sha256(segment):
    return _text_sha256((segment.get("text") or "").strip())


def _load_existing_evidence(transcript_path, metadata_path):
    transcript_exists = transcript_path.exists()
    metadata_exists = metadata_path.exists()
    if transcript_exists != metadata_exists:
        missing = metadata_path.name if transcript_exists else transcript_path.name
        raise RuntimeError(
            f"原始证据不完整，缺少 {missing}；"
            "请先恢复证据，或使用 --force-refetch 创建新 revision"
        )
    if not transcript_exists:
        return None, None
    transcript = transcript_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_hash = metadata.get("evidence", {}).get("transcript_sha256")
    if expected_hash and expected_hash != _text_sha256(transcript):
        raise RuntimeError(
            "原始转录与 transcript.raw.json 的 evidence hash 不一致，"
            "拒绝继续使用可能损坏的证据"
        )
    return transcript, metadata


def _archive_existing_evidence(folder, transcript_path, metadata_path):
    if not transcript_path.exists() and not metadata_path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    digest = (
        _source_sha256(transcript_path)[:8]
        if transcript_path.exists()
        else "missing"
    )
    archive = folder / "evidence_history" / f"{timestamp}-{digest}"
    archive.mkdir(parents=True, exist_ok=False)
    archived_files = []
    for path in (transcript_path, metadata_path):
        if path.exists():
            atomic_write_bytes(archive / path.name, path.read_bytes())
            archived_files.append(path.name)
    atomic_write_json(archive / "archive.json", {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "files": archived_files,
        "reason": "force_refetch",
    })
    return archive


def _prepare_evidence_metadata(metadata, transcript, folder=None):
    metadata = dict(metadata)
    segments = metadata.get("segments", [])
    for index, original in enumerate(segments, start=1):
        segment = dict(original)
        segment.setdefault("id", f"S{index:04d}")
        segment["content_sha256"] = _segment_text_sha256(segment)
        segments[index - 1] = segment
    metadata["segments"] = segments
    metadata.setdefault("meta", {})["transcript_chars"] = len(transcript)
    metadata["meta"]["transcript_file"] = "原始转录.txt"
    timestamped = metadata["meta"].get("timestamped")
    if timestamped is False:
        evidence_mode = "text_anchor"
    elif timestamped is True:
        evidence_mode = "timestamp"
    else:
        evidence_mode = (
            "timestamp"
            if any(segment.get("start") is not None for segment in segments)
            else "text_anchor"
        )
    metadata["meta"]["evidence_mode"] = evidence_mode
    metadata["evidence"] = {
        "schema_version": 1,
        "revision_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "integrity": "immutable_revision",
        "transcript_file": "原始转录.txt",
        "transcript_sha256": _text_sha256(transcript),
    }
    if folder is not None:
        metadata["provenance"] = build_provenance(folder, metadata)
    return metadata


def _source_identity(source):
    if not source:
        return ""
    if source.startswith(("http://", "https://")):
        parsed = urlsplit(source)
        return urlunsplit((
            "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        ))
    return os.path.abspath(source)


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
                     content_policy="faithful", display_title=None,
                     force_refetch=False, asr_language="en",
                     adaptive_refinement=True, align_audio=True):
    """抓取/读取转录，写入纯文本和可审计的 transcript.raw.json。"""
    transcript_path = folder / "原始转录.txt"
    metadata_path = folder / "transcript.raw.json"
    transcript = None
    metadata = None
    source_kind = "existing"
    write_evidence = False

    try:
        existing_transcript, existing_metadata = _load_existing_evidence(
            transcript_path, metadata_path)
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        if not force_refetch:
            raise
        print(
            f"[转录][警告] 现有 evidence revision 无法复用，将归档后重抓: "
            f"{exc}",
            flush=True,
        )
        existing_transcript, existing_metadata = None, None
    if existing_transcript is not None and not force_refetch:
        existing_source = existing_metadata.get("source", "")
        if (
                source
                and existing_source
                and _source_identity(source) != _source_identity(
                    existing_source)):
            raise RuntimeError(
                "同名单集目录已绑定不同 source；"
                "拒绝复用旧证据并改写来源，请确认 --name 或使用 --force-refetch"
            )
        transcript = existing_transcript
        metadata = existing_metadata
        source_kind = metadata.get("source_kind", source_kind)
        print(
            "[转录] 已存在原始 evidence revision，默认复用；"
            "需要重新抓取时显式使用 --force-refetch",
            flush=True,
        )

    if source and transcript is None:
        write_evidence = True
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
                            {
                                "start": None,
                                "end": None,
                                "text": chunk,
                                "synthetic_boundary": True,
                            }
                            for chunk in chunk_plain_transcript(transcript)
                        ],
                        "meta": {"timestamped": False, "source_warnings": detect_source_warnings(transcript)},
                    }
        elif os.path.isfile(source):
            if _is_audio_file(source):
                source_kind = "local_asr"
                context_texts = [Path(source).stem]
                existing_source_path = folder / "来源.md"
                if existing_source_path.exists():
                    context_texts.append(
                        existing_source_path.read_text(encoding="utf-8"))
                asr_context = build_asr_context(
                    title=display_title or name,
                    context_texts=context_texts,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords,
                )
                result = transcribe(
                    source, engine=engine, quality=quality,
                    asr_model=asr_model,
                    initial_prompt=initial_prompt, hotwords=hotwords,
                    lm_path=lm_path, diarize_audio=diarize_audio,
                    min_speakers=min_speakers, max_speakers=max_speakers,
                    language=asr_language,
                    asr_context=asr_context,
                    adaptive_refinement=adaptive_refinement,
                    align_audio=align_audio,
                    return_metadata=True,
                )
                transcript = result["text"]
                metadata = {
                    "source": os.path.abspath(source),
                    "source_kind": source_kind,
                    "source_sha256": _source_sha256(source),
                    **result,
                }
                if asr_context.initial_prompt and not initial_prompt:
                    print(
                        "  [ASR] 自动 initial_prompt: "
                        f"{asr_context.initial_prompt[:120]}",
                        flush=True,
                    )
            else:
                source_kind = "local_transcript"
                transcript = load_transcript_from_file(source)
                metadata = {
                    "source": os.path.abspath(source),
                    "source_kind": source_kind,
                    "segments": [
                        {
                            "start": None,
                            "end": None,
                            "text": chunk,
                            "synthetic_boundary": True,
                        }
                        for chunk in chunk_plain_transcript(transcript)
                    ],
                    "meta": {"timestamped": False, "source_warnings": detect_source_warnings(transcript)},
                }
        else:
            print(f"[错误] 无法识别输入源: {source}", flush=True)

    # 回退：读已有转录。不要因为已有文本而覆盖已有结构化结果。
    if (
            not transcript
            and existing_transcript is not None
            and not force_refetch):
        transcript = existing_transcript
        metadata = existing_metadata

    if not transcript or len(transcript) < 200:
        print("[错误] 无法获取转录文本", flush=True)
        return False

    if write_evidence and source_kind in {"web_transcript", "local_transcript"}:
        transcript = apply_content_policy(transcript, content_policy)
        if metadata:
            metadata.setdefault("meta", {})["content_policy"] = content_policy

    if write_evidence:
        if not metadata:
            raise RuntimeError("抓取成功但缺少结构化转录元数据")
        metadata = _prepare_evidence_metadata(
            metadata, transcript, folder=folder)
        archive = None
        if force_refetch:
            archive = _archive_existing_evidence(
                folder, transcript_path, metadata_path)
        atomic_write_text(transcript_path, transcript)
        atomic_write_json(metadata_path, metadata)
        print(
            f"[转录] 新 evidence revision: {len(transcript)} 字符 "
            f"→ {transcript_path.name}",
            flush=True,
        )
        print(f"[转录] 结构化结果 → {metadata_path.name}", flush=True)
        if archive:
            print(f"[转录] 旧 evidence revision 已归档 → {archive}", flush=True)
    else:
        print(
            f"[转录] 复用 {len(transcript)} 字符 → {transcript_path.name}",
            flush=True,
        )

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
                  "- pipeline 版本：v8", f"- ASR 质量：{quality}"]
        if metadata and metadata.get("meta", {}).get("diarization"):
            lines.append("- 说话人分离：已启用")
        atomic_write_text(source_path, "\n".join(lines))
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
        text = _upsert_source_field(text, "pipeline 版本", "v8")
        text = _upsert_source_field(text, "ASR 质量", quality)
        atomic_write_text(source_path, text)

    from episode import ensure_episode, sync_episode_state
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
    sync_episode_state(folder)

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


def _prepare_briefing_files(folder, briefing_path):
    """Normalize briefing text and keep summary-map hashes synchronized."""
    text = briefing_path.read_text(encoding="utf-8")
    summary_path = folder / "summary_map.json"
    summary_map = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.exists() else {"chapters": []}
    )
    normalized, summary_map, normalization_changes = (
        normalize_briefing_artifacts(text, summary_map)
    )
    fixed, issues, validation = validate_and_fix(
        normalized, return_details=True)
    fixed, summary_map, post_validation_changes = (
        normalize_briefing_artifacts(fixed, summary_map)
    )
    normalization_changes.extend(
        change for change in post_validation_changes
        if change not in normalization_changes
    )
    if fixed != text:
        atomic_write_text(briefing_path, fixed)
    if summary_path.exists():
        atomic_write_json(summary_path, summary_map)
    return text, fixed, issues, validation, normalization_changes


def _validate_finalized_briefing_files(folder, briefing_path):
    """Validate canonical content without mutating review-bound artifacts."""
    folder = Path(folder)
    text = briefing_path.read_text(encoding="utf-8")
    summary_path = folder / "summary_map.json"
    if not summary_path.exists():
        return ["缺少 summary_map.json，不能验证审查后的只读内容"], {}
    try:
        summary_map = json.loads(summary_path.read_text(encoding="utf-8"))
        finalized, aligned, changes = finalize_content_artifacts(
            text, summary_map)
    except (json.JSONDecodeError, ContentFinalizationError, ValueError) as exc:
        return [f"内容最终化校验失败: {exc}"], {}
    errors = []
    if finalized != text:
        errors.append(
            "讲书稿.md 尚未最终化；TTS/HTML 阶段禁止自动修改审查输入")
    if aligned != summary_map:
        errors.append(
            "summary_map.json 尚未最终化或哈希已过期；"
            "TTS/HTML 阶段禁止自动修改审查输入")
    tts_issues = validate_tts_readiness(
        text, load_tts_lexicon(folder))
    errors.extend(tts_issues)
    return errors, {
        "normalization_changes_required": changes,
        "briefing_sha256": _text_sha256(text),
        "summary_sha256": _text_sha256(
            summary_path.read_text(encoding="utf-8")),
        "tts_readiness_issue_count": len(tts_issues),
    }


def _run_quality_gate(
        folder, auto_ai_review=True, allow_legacy=False, run_report=None):
    """Compatibility wrapper around the shared preflight implementation."""
    return shared_quality_gate(
        folder,
        auto_ai_review=auto_ai_review,
        allow_legacy=allow_legacy,
        run_report=run_report,
    )


def run_tts_step(folder, name, briefing_file, tts_speed, force_tts, read_titles,
                 auto_ai_review=True, allow_legacy=False, run_report=None):
    with _stage(run_report, "tts_config_preflight"):
        validate_for_stage("tts")
    # 内容最终化必须由上游内容阶段显式完成。TTS 及 HTML 只读验证，
    # 避免审查通过后再修改讲稿/summary_map 的 TOCTOU 问题。
    with _stage(run_report, "validate_finalized_content") as stage:
        briefing_path = folder / briefing_file
        if briefing_path.exists():
            text = briefing_path.read_text(encoding="utf-8")
            _run_structure_check(text)
            errors, metrics = _validate_finalized_briefing_files(
                folder, briefing_path)
            if stage is not None:
                stage.metrics.update(metrics)
                if errors:
                    stage.fail("; ".join(errors[:5]))
            if errors:
                for error in errors:
                    print(f"[内容只读校验][阻断] {error}", flush=True)
                return False

    with _stage(run_report, "quality_gate") as stage:
        quality_ok = _run_quality_gate(
            folder,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy,
            run_report=run_report)
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
    with _stage(run_report, "prepare_release") as stage:
        release = prepare_release(
            folder,
            folder / f"{name}.mp3",
            folder / briefing_file,
        )
        if stage is not None:
            stage.metrics.update({
                "release_id": release.get("release_id"),
                "audio_key": release.get("audio_key"),
            })
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
    """生成 HTML 阅读页面；质量门之后不修改内容源文件。"""
    md_path = folder / briefing_file
    if not md_path.exists():
        print(f"[HTML] 跳过：找不到 {md_path}", flush=True)
        return False
    with _stage(run_report, "html_quality_gate") as stage:
        text = md_path.read_text(encoding="utf-8")
        _run_structure_check(text)
        errors, metrics = _validate_finalized_briefing_files(folder, md_path)
        if stage is not None:
            stage.metrics.update(metrics)
            if errors:
                stage.fail("; ".join(errors[:5]))
        if errors:
            for error in errors:
                print(f"[内容只读校验][阻断] {error}", flush=True)
            return False
        quality_ok = _run_quality_gate(
            folder,
            auto_ai_review=auto_ai_review,
            allow_legacy=allow_legacy,
            run_report=run_report)
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
            "  默认流程会先调用 subagent 生成 转录_纠错.txt，再继续写稿。\n",
            flush=True,
        )


def _process_impl(source, name, folder, run_report, options):
    asr_model = options.asr_model
    tts_speed = options.tts_speed
    force_tts = options.force_tts
    read_titles = options.read_titles
    fetch_only = options.fetch_only
    tts_only = options.tts_only
    html_only = options.html_only
    no_html = options.no_html
    quality = options.quality
    initial_prompt = options.initial_prompt
    hotwords = options.hotwords
    engine = options.engine
    lm_path = options.lm_path
    diarize_audio = options.diarize_audio
    min_speakers = options.min_speakers
    max_speakers = options.max_speakers
    content_policy = options.content_policy
    auto_ai_review = options.auto_ai_review
    allow_legacy = options.allow_legacy
    display_title = options.display_title
    force_refetch = options.force_refetch
    asr_language = options.asr_language
    auto_content = options.auto_content
    adaptive_refinement = options.adaptive_refinement
    align_audio = options.align_audio

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
                  f"先运行 subagent 内容编排生成讲书稿。", flush=True)
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
            display_title=display_title,
            force_refetch=force_refetch,
            asr_language=asr_language,
            adaptive_refinement=adaptive_refinement,
            align_audio=align_audio)
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
                "transport": raw.get("meta", {}).get("transport"),
                "retry_count": raw.get("meta", {}).get(
                    "fetch_retry_count", 0),
                "tls_downgrade": bool(
                    raw.get("meta", {}).get("tls_downgrade")),
                "asr_refinement_ranges": raw.get(
                    "meta", {}).get(
                        "adaptive_refinement", {}).get(
                            "candidate_ranges", 0),
                "asr_refinement_accepted": raw.get(
                    "meta", {}).get(
                        "adaptive_refinement", {}).get(
                            "accepted_ranges", 0),
                "asr_refinement_remaining": raw.get(
                    "meta", {}).get(
                        "adaptive_refinement", {}).get(
                            "remaining_segments", 0),
                "alignment_status": raw.get(
                    "meta", {}).get("alignment", {}).get("status"),
                "alignment_coverage": raw.get(
                    "meta", {}).get(
                        "alignment", {}).get(
                            "word_timestamp_coverage"),
                "diarization_exclusive": raw.get(
                    "meta", {}).get(
                        "diarization_meta", {}).get(
                            "exclusive_used"),
            })
        if stage is not None and not fetched:
            stage.fail("transcript fetch failed")
        if not fetched:
            return False
    _warn_if_asr_needs_correction(folder)

    # ---- 抓取后：默认由 subagent 自动生成内容 ----
    briefing_file, briefing_path = detect_briefing(folder)
    needs_content = (
        not fetch_only
        and auto_content
        and content_pipeline_needed(folder, force=force_refetch)
    )
    if needs_content:
        print("[内容] 内容产物缺失、过期或不完整，启动 subagent 编排...", flush=True)
        if not run_content_pipeline(
                folder,
                display_title or name,
                run_report,
                force=force_refetch):
            print("[内容][阻断] subagent 内容编排失败", flush=True)
            return False
        briefing_file, briefing_path = detect_briefing(folder)
    if fetch_only or not briefing_path:
        print(
            "\n[下一步] 转录已就绪。请运行 subagent 内容编排，或手动生成\n"
            f"  {folder / '原始转录.txt'}\n"
            "并生成 content_map.json、中文完整笔记.md、讲书稿.md、summary_map.json，"
            "完成后运行：\n"
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


def process_episode(source, name, options):
    """Run one episode through the deep process interface."""
    folder = BASE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(
            f"episode:{folder.resolve()}", blocking=False):
        report = RunReport(
            folder, "process", options.run_metadata(source))
        try:
            ok = _process_impl(source, name, folder, report, options)
        except Exception as exc:
            report.finish(False, exc)
            raise
        report.finish(ok, None if ok else "pipeline returned failure")
        return ok


def process(source, name, asr_model=None, tts_speed=1.0,
            force_tts=False, read_titles=True, fetch_only=False, tts_only=False,
            html_only=False, no_html=False, quality="balanced",
            initial_prompt=None, hotwords=None, engine="whisper",
            lm_path=None, diarize_audio=True,
            min_speakers=None, max_speakers=None,
            content_policy="faithful", auto_ai_review=True,
            allow_legacy=False, display_title=None, force_refetch=False,
            asr_language="en", auto_content=True, adaptive_refinement=True,
            align_audio=True):
    """Backward-compatible adapter; use process_episode + EpisodeOptions internally."""
    return process_episode(source, name, EpisodeOptions(
        asr_model=asr_model,
        tts_speed=tts_speed,
        force_tts=force_tts,
        read_titles=read_titles,
        fetch_only=fetch_only,
        tts_only=tts_only,
        html_only=html_only,
        no_html=no_html,
        quality=quality,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        engine=engine,
        lm_path=lm_path,
        diarize_audio=diarize_audio,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        content_policy=content_policy,
        auto_ai_review=auto_ai_review,
        allow_legacy=allow_legacy,
        display_title=display_title,
        force_refetch=force_refetch,
        asr_language=asr_language,
        auto_content=auto_content,
        adaptive_refinement=adaptive_refinement,
        align_audio=align_audio,
    ))


# ===================================================================
#  命令行入口
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description="播客处理流水线 v8（抓取、严格质量门、TTS 与 HTML）"
    )
    parser.add_argument("source", nargs="?",
                        help="URL / mp3 / 转录文件路径（--tts-only 时可省略）")
    parser.add_argument("--name", default=None, help="播客文件夹名称（不给则从 URL 自动提取标题；命名即原始标题）")
    parser.add_argument("--transcript", help="直接指定转录文件路径")
    parser.add_argument("--fetch-only", action="store_true",
                        help="只抓转录，不跑内容生成、TTS 或 HTML")
    parser.add_argument(
        "--no-auto-content",
        action="store_true",
        help="抓取后停在原始转录，禁用 subagent 内容编排",
    )
    parser.add_argument("--tts-only", action="store_true",
                        help="跳过抓取，对已有讲稿直接跑 TTS")
    parser.add_argument("--html-only", action="store_true",
                        help="跳过抓取和 TTS，仅从已有讲稿生成 HTML")
    parser.add_argument("--no-html", action="store_true",
                        help="TTS 后不自动生成 HTML")
    parser.add_argument("--asr-model", default=None,
                        help="Whisper 模型大小（不传则由 ASR 质量预设决定；可选 large-v3/large-v3-turbo/medium）")
    parser.add_argument(
        "--asr-language",
        default="en",
        help="Whisper 语言代码，默认 en；传 auto 启用自动检测",
    )
    parser.add_argument("--asr-quality", default="balanced",
                        choices=["fast", "balanced", "max"],
                        help="ASR 质量预设: fast(快速/medium) / balanced(默认/large-v3-turbo) / max(复核/large-v3+全调优)")
    parser.add_argument("--asr-engine", default="whisper",
                        choices=["whisper", "whisper-fast"],
                        help="ASR 引擎: whisper(默认) / whisper-fast（快速预设）")
    parser.add_argument("--lm", default=None,
                        help="保留兼容性的语言模型参数（当前 Whisper 路径不使用）")
    parser.add_argument("--diarize", action="store_true", default=True,
                        help="启用 pyannote 说话人分离（默认启用；缺 HF_TOKEN 时自动跳过）")
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
    parser.add_argument(
        "--no-asr-refine",
        action="store_true",
        help="关闭低置信片段定向重解码，保留首轮 ASR 结果",
    )
    parser.add_argument(
        "--no-align",
        action="store_true",
        help="关闭 WhisperX 强制对齐，保留 Whisper 原始词时间戳",
    )

    parser.add_argument("--content-policy", default="faithful",
                        choices=["faithful", "no-ads", "summary-ready"],
                        help="网页/本地文本的编辑策略；默认 faithful 不静默删除内容")
    parser.add_argument("--skip-ai-review", action="store_true",
                        help="不自动调用 subagent 审查，只校验已有 ai_review.json")
    parser.add_argument(
        "--allow-legacy-quality",
        action="store_true",
        help="显式允许缺少 content_map.json 的旧期绕过完整质量门",
    )
    parser.add_argument(
        "--force-refetch",
        action="store_true",
        help="显式抓取新 evidence revision；旧原始证据会先归档",
    )
    parser.add_argument(
        "--upgrade-asr",
        action="store_true",
        help=(
            "使用单集目录中的 原始音频 以 max 质量重新 ASR；"
            "自动归档旧 evidence 并重建下游内容"
        ),
    )

    args = parser.parse_args()

    source = args.source
    if args.transcript:
        source = args.transcript
    if (
            not source
            and not args.tts_only
            and not args.html_only
            and not args.upgrade_asr):
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
    if args.upgrade_asr:
        candidates = original_audio_files(BASE_DIR / name)
        if not candidates:
            print(
                f"[错误] {BASE_DIR / name} 中找不到包含“原始音频”的音频文件",
                flush=True,
            )
            sys.exit(1)
        source = str(candidates[0])
        args.force_refetch = True
        args.asr_quality = "max"
        print(
            f"[ASR升级] 使用 {candidates[0].name} 创建新 evidence revision",
            flush=True,
        )

    # 说话人分离：--diarize 默认启用，--no-diarize 可覆盖
    _diarize = args.diarize and not args.no_diarize
    options = EpisodeOptions(
        asr_model=args.asr_model,
        tts_speed=args.tts_speed,
        force_tts=args.force_tts,
        read_titles=not args.no_tts_titles,
        fetch_only=args.fetch_only,
        tts_only=args.tts_only,
        html_only=args.html_only,
        no_html=args.no_html,
        quality=args.asr_quality,
        initial_prompt=args.initial_prompt,
        hotwords=args.hotwords,
        engine=args.asr_engine,
        lm_path=args.lm,
        diarize_audio=_diarize,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        content_policy=args.content_policy,
        auto_ai_review=not args.skip_ai_review,
        allow_legacy=args.allow_legacy_quality,
        display_title=raw_title,
        force_refetch=args.force_refetch,
        asr_language=(
            None if args.asr_language.lower() == "auto"
            else args.asr_language
        ),
        auto_content=not args.no_auto_content,
        adaptive_refinement=not args.no_asr_refine,
        align_audio=not args.no_align,
    )
    ok = process_episode(source, name, options)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

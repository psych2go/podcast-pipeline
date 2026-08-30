"""Argument parsing and deterministic CLI-to-options conversion."""
import argparse

from .options import EpisodeOptions


def build_process_parser() -> argparse.ArgumentParser:
    """Build the stable ``scripts/process.py`` command-line interface."""
    parser = argparse.ArgumentParser(
        description="播客处理流水线 v8（抓取、严格质量门、TTS 与 HTML）"
    )
    parser.add_argument(
        "source", nargs="?",
        help="URL / mp3 / 转录文件路径（--tts-only 时可省略）",
    )
    parser.add_argument(
        "--name", default=None,
        help="播客文件夹名称（不给则从 URL 自动提取标题；命名即原始标题）",
    )
    parser.add_argument(
        "--official-url", default=None,
        help="官方播客页面；与 positional 字幕/音频证据 URL 分开记录",
    )
    parser.add_argument("--transcript", help="直接指定转录文件路径")
    parser.add_argument(
        "--fetch-only", action="store_true",
        help="只抓转录，不跑内容生成、TTS 或 HTML",
    )
    parser.add_argument(
        "--no-auto-content", action="store_true",
        help="抓取后停在原始转录，禁用 subagent 内容编排",
    )
    parser.add_argument(
        "--tts-only", action="store_true",
        help="跳过抓取，对已有讲稿直接跑 TTS",
    )
    parser.add_argument(
        "--html-only", action="store_true",
        help="跳过抓取和 TTS，仅从已有讲稿生成 HTML",
    )
    parser.add_argument(
        "--no-html", action="store_true",
        help="TTS 后不自动生成 HTML",
    )
    parser.add_argument(
        "--asr-model", default=None,
        help=(
            "Whisper 模型大小（不传则由 ASR 质量预设决定；"
            "可选 large-v3/large-v3-turbo/medium）"
        ),
    )
    parser.add_argument(
        "--asr-language", default="en",
        help="Whisper 语言代码，默认 en；传 auto 启用自动检测",
    )
    parser.add_argument(
        "--asr-quality", default="balanced",
        choices=["fast", "balanced", "max"],
        help=(
            "ASR 质量预设: fast(快速/medium) / "
            "balanced(默认/large-v3-turbo) / max(复核/large-v3+全调优)"
        ),
    )
    parser.add_argument(
        "--asr-engine", default="whisper",
        choices=["whisper", "whisper-fast"],
        help="ASR 引擎: whisper(默认) / whisper-fast（快速预设）",
    )
    parser.add_argument(
        "--lm", default=None,
        help="保留兼容性的语言模型参数（当前 Whisper 路径不使用）",
    )
    parser.add_argument(
        "--diarize", action="store_true", default=True,
        help="启用 pyannote 说话人分离（默认启用；缺 HF_TOKEN 时自动跳过）",
    )
    parser.add_argument(
        "--no-diarize", action="store_true",
        help="跳过说话人分离（省时，无 SPEAKER 标签）",
    )
    parser.add_argument(
        "--min-speakers", type=int, default=None,
        help="说话人分离：最少说话人数",
    )
    parser.add_argument(
        "--max-speakers", type=int, default=None,
        help="说话人分离：最多说话人数",
    )
    parser.add_argument(
        "--tts-speed", type=float, default=1.0,
        help="TTS 语速（默认 1.0）",
    )
    parser.add_argument(
        "--force-tts", action="store_true",
        help="TTS 清空旧音频重新生成（默认断点续传）",
    )
    parser.add_argument(
        "--no-tts-titles", action="store_true",
        help="不在音频中朗读章节标题",
    )
    parser.add_argument(
        "--initial-prompt", default=None,
        help="MP3 转录用：已知人名/公司/术语/背景，条件化 Whisper 减少专有名词错听",
    )
    parser.add_argument(
        "--hotwords", default=None,
        help="MP3 转录用：逗号分隔的热词，加权特定词（如 'Naval,Gary Tan,Claude Code'）",
    )
    parser.add_argument(
        "--no-asr-refine", action="store_true",
        help="关闭低置信片段定向重解码，保留首轮 ASR 结果",
    )
    parser.add_argument(
        "--no-align", action="store_true",
        help="关闭 WhisperX 强制对齐，保留 Whisper 原始词时间戳",
    )
    parser.add_argument(
        "--content-policy", default="faithful",
        choices=["faithful", "no-ads", "summary-ready"],
        help="网页/本地文本的编辑策略；默认 faithful 不静默删除内容",
    )
    parser.add_argument(
        "--skip-ai-review", action="store_true",
        help="不自动调用 subagent 审查，只校验已有 ai_review.json",
    )
    parser.add_argument(
        "--allow-legacy-quality", action="store_true",
        help="显式允许缺少 content_map.json 的旧期绕过完整质量门",
    )
    parser.add_argument(
        "--force-refetch", action="store_true",
        help="显式抓取新 evidence revision；旧原始证据会先归档",
    )
    parser.add_argument(
        "--upgrade-asr", action="store_true",
        help=(
            "使用单集目录中的 原始音频 以 max 质量重新 ASR；"
            "自动归档旧 evidence 并重建下游内容"
        ),
    )
    return parser


def episode_options_from_args(
        args: argparse.Namespace, *, display_title: str | None,
        official_url: str | None, diarize_audio: bool) -> EpisodeOptions:
    """Convert already-resolved CLI values into the orchestrator contract."""
    return EpisodeOptions(
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
        diarize_audio=diarize_audio,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        content_policy=args.content_policy,
        auto_ai_review=not args.skip_ai_review,
        allow_legacy=args.allow_legacy_quality,
        display_title=display_title,
        official_url=official_url,
        force_refetch=args.force_refetch,
        asr_language=(
            None if args.asr_language.lower() == "auto"
            else args.asr_language
        ),
        auto_content=not args.no_auto_content,
        adaptive_refinement=not args.no_asr_refine,
        align_audio=not args.no_align,
    )

"""Stable parameter object for single-episode processing."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EpisodeOptions:
    """Options passed from the CLI facade into the episode orchestrator."""

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
    official_url: str | None = None
    force_refetch: bool = False
    asr_language: str | None = "en"
    auto_content: bool = True
    adaptive_refinement: bool = True
    align_audio: bool = True

    @property
    def mode(self) -> str:
        if self.html_only:
            return "html-only"
        if self.tts_only:
            return "tts-only"
        if self.fetch_only:
            return "fetch-only"
        return "full"

    def run_metadata(self, source) -> dict:
        return {
            "mode": self.mode,
            "source": source if source and str(source).startswith("http") else (
                str(source) if source else ""),
            "official_url": self.official_url or "",
            "asr_quality": self.quality,
            "asr_model": self.asr_model,
            "asr_language": self.asr_language,
            "force_refetch": self.force_refetch,
            "adaptive_refinement": self.adaptive_refinement,
            "align_audio": self.align_audio,
            "tts_speed": self.tts_speed,
            "auto_content": self.auto_content,
        }

"""Declarative map of the supported end-to-end pipeline.

This module is navigation metadata, not an alternate orchestrator. Runtime order
remains owned by ``process.py``, ``agent_pipeline.py``, and ``catalog.py``.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StageSpec:
    key: str
    title: str
    owners: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    updates: tuple[str, ...] = ()
    public_entrypoint: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


STAGES = (
    StageSpec(
        key="source-acquisition",
        title="来源抓取、ASR 与 evidence revision",
        owners=("fetcher.py", "episode.py", "evidence.py"),
        inputs=("source URL / MP3 / transcript file",),
        outputs=("episode.json", "来源.md", "原始转录.txt", "transcript.raw.json"),
        public_entrypoint="scripts/process.py",
    ),
    StageSpec(
        key="transcript-correction",
        title="逐 segment 转录纠错",
        owners=("agent_pipeline.py", "transcript_correction.py"),
        inputs=("transcript.raw.json", "原始转录.txt"),
        outputs=("correction_manifest.json", "转录_纠错.txt"),
    ),
    StageSpec(
        key="content-map",
        title="内容单元与 source accountability",
        owners=("agent_pipeline.py", "content_map.py"),
        inputs=("transcript.raw.json", "转录_纠错.txt"),
        outputs=("content_map.json",),
    ),
    StageSpec(
        key="claim-evidence",
        title="逐 claim 最小证据绑定",
        owners=("claim_evidence.py",),
        inputs=("content_map.json", "transcript.raw.json", "correction_manifest.json"),
        outputs=("claim_evidence_progress.json",),
        updates=("content_map.json",),
    ),
    StageSpec(
        key="canonical-entities",
        title="规范实体与公开名称合同",
        owners=("agent_pipeline.py", "canonical_entities.py"),
        inputs=("content_map.json", "transcript.raw.json", "转录_纠错.txt"),
        outputs=("canonical_entities.json",),
        updates=("content_map.json",),
    ),
    StageSpec(
        key="prewrite-fact-checks",
        title="写作前原子事实核查",
        owners=("prewrite_fact_checks.py",),
        inputs=("content_map.json", "canonical_entities.json", "转录_纠错.txt"),
        outputs=(
            "editorial_fact_checks.json",
            "editorial_fact_check_batches/",
            "prewrite_fact_checks_progress.json",
        ),
    ),
    StageSpec(
        key="content-writing",
        title="中文完整笔记、讲稿与章节映射",
        owners=("agent_pipeline.py",),
        inputs=(
            "content_map.json",
            "canonical_entities.json",
            "editorial_fact_checks.json",
        ),
        outputs=("中文完整笔记.md", "讲书稿.md", "summary_map.json"),
    ),
    StageSpec(
        key="content-finalization",
        title="确定性内容最终化",
        owners=("content_finalizer.py",),
        inputs=("中文完整笔记.md", "讲书稿.md", "summary_map.json"),
        outputs=("tts_lexicon.json",),
        updates=("讲书稿.md", "summary_map.json", "content_map.json"),
    ),
    StageSpec(
        key="review-and-quality",
        title="AI 审查、受限修复与确定性质量门",
        owners=(
            "ai_review.py",
            "review_repair.py",
            "quality_report.py",
            "preflight.py",
        ),
        inputs=(
            "content_map.json",
            "editorial_fact_checks.json",
            "中文完整笔记.md",
            "讲书稿.md",
            "summary_map.json",
        ),
        outputs=("ai_review.json", "review_repair.json", "quality_report.json"),
    ),
    StageSpec(
        key="tts",
        title="TTS 合成与音频 manifest",
        owners=("tts.py", "sections.py"),
        inputs=("quality_report.json", "讲书稿.md", "tts_lexicon.json"),
        outputs=("audio/", "tts_manifest.json", "<storage_name>.mp3"),
    ),
    StageSpec(
        key="release-preparation",
        title="内容哈希音频 key 与 release provenance 准备",
        owners=("process.py", "release.py"),
        inputs=("quality_report.json", "讲书稿.md", "tts_manifest.json", "<storage_name>.mp3"),
        outputs=("release.json",),
    ),
    StageSpec(
        key="reader-page",
        title="单集阅读页生成",
        owners=("html_gen.py", "sections.py", "episode.py"),
        inputs=("quality_report.json", "讲书稿.md", "episode.json", "release.json"),
        outputs=("<storage_name> - content.html",),
    ),
    StageSpec(
        key="release-and-publish",
        title="发布事务、R2、Pages 与远端验收",
        owners=("catalog.py", "catalog_publish.py", "publish.py"),
        inputs=(
            "release.json",
            "quality_report.json",
            "tts_manifest.json",
            "<storage_name>.mp3",
            "<storage_name> - content.html",
        ),
        outputs=("publish_report.json", "site/site.json"),
        updates=("release.json",),
        public_entrypoint="scripts/catalog.py finish / finish-batch",
    ),
)


def stage_by_key(key: str) -> StageSpec:
    for stage in STAGES:
        if stage.key == key:
            return stage
    raise KeyError(key)


def artifact_producers(artifact: str) -> tuple[StageSpec, ...]:
    return tuple(stage for stage in STAGES if artifact in stage.outputs)


def artifact_consumers(artifact: str) -> tuple[StageSpec, ...]:
    return tuple(stage for stage in STAGES if artifact in stage.inputs)


def validate_stage_map() -> list[str]:
    errors = []
    keys = [stage.key for stage in STAGES]
    if len(keys) != len(set(keys)):
        errors.append("pipeline stage key 必须唯一")
    for stage in STAGES:
        if not stage.owners:
            errors.append(f"{stage.key} 缺少 owner")
        if not stage.outputs:
            errors.append(f"{stage.key} 缺少 output")
    return errors

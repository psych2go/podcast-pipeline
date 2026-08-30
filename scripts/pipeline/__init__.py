"""Stable pipeline contracts and navigation metadata."""
from .cli import build_process_parser, episode_options_from_args
from .options import EpisodeOptions
from .stages import (
    STAGES,
    StageSpec,
    artifact_consumers,
    artifact_producers,
    stage_by_key,
    validate_stage_map,
)

__all__ = [
    "EpisodeOptions",
    "STAGES",
    "StageSpec",
    "artifact_consumers",
    "artifact_producers",
    "build_process_parser",
    "episode_options_from_args",
    "stage_by_key",
    "validate_stage_map",
]

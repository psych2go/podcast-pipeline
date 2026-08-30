"""Evidence provenance and legacy ASR migration."""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

try:
    from hashing import sha256_file as _sha256_file
except ImportError:
    from scripts.hashing import sha256_file as _sha256_file

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json


PROVENANCE_SCHEMA_VERSION = 1
ASR_SOURCE_KINDS = frozenset({"local_asr", "legacy_asr"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"})


def _audio_duration_seconds(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            duration = float(result.stdout.strip())
            return round(duration, 3) if duration > 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def original_audio_files(folder):
    folder = Path(folder)
    matches = []
    for path in folder.iterdir() if folder.exists() else ():
        if (
                path.is_file()
                and path.suffix.lower() in _AUDIO_SUFFIXES
                and "原始音频" in path.name):
            matches.append(path)
    return sorted(matches, key=lambda path: path.name)


def effective_source_kind(folder, raw=None):
    """Return the evidence origin used by quality gates.

    Explicit provenance wins. Legacy folders with an original audio file and
    an imported plain transcript are classified as ``legacy_asr``.
    """
    folder = Path(folder)
    raw = raw or {}
    provenance = raw.get("provenance")
    if isinstance(provenance, dict):
        explicit = provenance.get("origin_kind")
        if explicit:
            return explicit

    stored = raw.get("source_kind", "")
    if stored in ASR_SOURCE_KINDS:
        return stored
    if (
            original_audio_files(folder)
            and stored in {"", "existing", "local_transcript"}):
        return "legacy_asr"
    return stored or "unknown"


def build_provenance(folder, raw):
    folder = Path(folder)
    raw = dict(raw or {})
    existing = raw.get("provenance")
    provenance = dict(existing) if isinstance(existing, dict) else {}
    stored_kind = raw.get("source_kind", "")
    origin_kind = effective_source_kind(folder, raw)
    provenance.update({
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "origin_kind": origin_kind,
        "inferred": bool(
            not existing
            and origin_kind == "legacy_asr"
            and stored_kind != origin_kind
        ),
        "source": raw.get("source", ""),
    })

    audio_files = original_audio_files(folder)
    if audio_files:
        audio = audio_files[0]
        provenance["original_audio"] = {
            "file": audio.name,
            "sha256": _sha256_file(audio),
            "size_bytes": audio.stat().st_size,
            "duration_seconds": _audio_duration_seconds(audio),
        }
    elif raw.get("source_sha256") and origin_kind in ASR_SOURCE_KINDS:
        provenance["original_audio"] = {
            "file": raw.get("source", ""),
            "sha256": raw["source_sha256"],
            "size_bytes": None,
            "duration_seconds": None,
        }

    if origin_kind in ASR_SOURCE_KINDS:
        meta = raw.get("meta", {})
        existing_asr = provenance.get("asr")
        asr = dict(existing_asr) if isinstance(existing_asr, dict) else {}
        asr.update({
            "engine": meta.get("engine") or asr.get("engine") or (
                "unknown" if origin_kind == "legacy_asr" else "faster-whisper"
            ),
            "model": meta.get("model") or asr.get("model") or "unknown",
            "quality": meta.get("quality") or asr.get("quality") or "unknown",
            "language": meta.get("language") or asr.get("language"),
            "requested_language": (
                meta.get("requested_language")
                or asr.get("requested_language")
            ),
            "diarization": bool(
                meta.get("diarization", asr.get("diarization", False))),
            "timestamped": bool(meta.get("timestamped", False)),
        })
        alignment = meta.get("alignment")
        if isinstance(alignment, dict):
            asr["alignment"] = {
                key: alignment.get(key)
                for key in (
                    "enabled",
                    "adapter",
                    "status",
                    "model",
                    "device",
                    "word_timestamp_coverage",
                )
                if key in alignment
            }
        diarization_meta = meta.get("diarization_meta")
        if isinstance(diarization_meta, dict):
            asr["diarization_details"] = {
                key: diarization_meta.get(key)
                for key in (
                    "model",
                    "device",
                    "exclusive_requested",
                    "exclusive_used",
                    "speaker_count",
                    "turn_count",
                )
                if key in diarization_meta
            }
        refinement = meta.get("adaptive_refinement")
        if isinstance(refinement, dict):
            asr["adaptive_refinement"] = {
                key: refinement.get(key)
                for key in (
                    "enabled",
                    "selected_segments",
                    "candidate_ranges",
                    "accepted_ranges",
                    "rejected_ranges",
                    "failed_ranges",
                    "remaining_segments",
                )
                if key in refinement
            }
        completeness = meta.get("completeness")
        if isinstance(completeness, dict):
            asr["completeness"] = {
                key: completeness.get(key)
                for key in (
                    "schema_version", "status", "detector", "passed",
                    "audio_duration_seconds", "speech_coverage",
                    "max_uncovered_speech_seconds", "timeline_valid",
                    "enforcement_mode",
                )
                if key in completeness
            }
        for key in (
                "completeness_contract_version",
                "correction_contract_version",
                "source_accountability_contract_version",
                "completeness_mode"):
            if meta.get(key) is not None:
                asr[key] = meta[key]
        provenance["asr"] = asr
    else:
        provenance.pop("asr", None)
    return provenance


def migrate_evidence_provenance(folder):
    """Add provenance to an existing transcript revision without changing text."""
    folder = Path(folder)
    path = folder / "transcript.raw.json"
    if not path.exists():
        raise FileNotFoundError(path)
    current_raw_sha256 = _sha256_file(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    provenance = build_provenance(folder, raw)
    migration = provenance.get("migration")
    if not isinstance(migration, dict):
        previous_raw_sha256 = current_raw_sha256
        review_path = folder / "ai_review.json"
        if review_path.exists():
            try:
                review = json.loads(
                    review_path.read_text(encoding="utf-8"))
                previous_raw_sha256 = review.get(
                    "reviewed_files", {}).get(
                    "transcript.raw.json", previous_raw_sha256)
            except (OSError, json.JSONDecodeError):
                pass
        provenance["migration"] = {
            "kind": "metadata_only",
            "previous_raw_sha256": previous_raw_sha256,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }
    raw["provenance"] = provenance
    if provenance["origin_kind"] == "legacy_asr":
        raw["source_kind"] = "legacy_asr"
    atomic_write_json(path, raw)
    return raw


def validate_provenance(folder, raw):
    folder = Path(folder)
    raw = raw or {}
    errors = []
    warnings = []
    stored_kind = raw.get("source_kind", "")
    kind = effective_source_kind(folder, raw)
    provenance = raw.get("provenance")

    if not isinstance(provenance, dict):
        warnings.append("transcript.raw.json 缺少结构化 provenance")
    else:
        if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
            errors.append("evidence provenance schema 不受支持")
        explicit = provenance.get("origin_kind")
        if explicit and stored_kind and explicit != stored_kind:
            errors.append(
                "transcript.raw.json.source_kind 与 provenance.origin_kind 不一致")

    if stored_kind != kind:
        warnings.append(
            f"转录来源类型已推断为 {kind}，原记录 {stored_kind or 'unknown'} 已过期")

    if original_audio_files(folder) and kind not in ASR_SOURCE_KINDS:
        errors.append(
            "目录存在原始音频，但 provenance 未标记为 local_asr/legacy_asr")

    if kind in ASR_SOURCE_KINDS:
        audio = (
            provenance.get("original_audio")
            if isinstance(provenance, dict) else None
        )
        if not isinstance(audio, dict) or not audio.get("sha256"):
            errors.append("ASR evidence 缺少原始音频 SHA-256")
        asr = provenance.get("asr") if isinstance(provenance, dict) else None
        if not isinstance(asr, dict):
            errors.append("ASR evidence 缺少模型与参数 provenance")
        elif kind == "local_asr" and asr.get("model") in {None, "", "unknown"}:
            errors.append("本地 ASR evidence 缺少实际模型记录")
        elif kind == "legacy_asr" and asr.get("model") in {None, "", "unknown"}:
            warnings.append("历史 ASR 未保留实际模型，不能计算可复现性")
    return errors, warnings


def correction_metrics(folder):
    """Return deterministic raw/corrected transcript drift metrics."""
    folder = Path(folder)
    raw_path = folder / "原始转录.txt"
    corrected_path = folder / "转录_纠错.txt"
    if not raw_path.exists() or not corrected_path.exists():
        return None
    raw_words = raw_path.read_text(encoding="utf-8").split()
    corrected_words = corrected_path.read_text(encoding="utf-8").split()
    matcher = SequenceMatcher(
        None, raw_words, corrected_words, autojunk=False)
    changed_blocks = 0
    changed_words = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed_blocks += 1
            changed_words += max(i2 - i1, j2 - j1)
    return {
        "raw_words": len(raw_words),
        "corrected_words": len(corrected_words),
        "matching_ratio": round(matcher.ratio(), 6),
        "changed_blocks": changed_blocks,
        "changed_words": changed_words,
    }


def main():
    parser = argparse.ArgumentParser(description="转录 evidence provenance 管理")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser(
        "migrate", help="补齐 provenance 并识别历史 ASR")
    migrate.add_argument("folder")
    check = sub.add_parser("check", help="校验 evidence provenance")
    check.add_argument("folder")
    args = parser.parse_args()

    folder = Path(args.folder)
    if args.command == "migrate":
        payload = migrate_evidence_provenance(folder)
        print(
            f"[evidence] {folder.name}: "
            f"origin_kind={payload['provenance']['origin_kind']}")
        return 0

    raw = json.loads(
        (folder / "transcript.raw.json").read_text(encoding="utf-8"))
    errors, warnings = validate_provenance(folder, raw)
    for warning in warnings:
        print(f"[evidence][警告] {warning}")
    for error in errors:
        print(f"[evidence][错误] {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

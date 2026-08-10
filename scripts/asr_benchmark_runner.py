"""Run ASR policies against a benchmark manifest and recommend a policy."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import time
from pathlib import Path

try:
    from asr_benchmark import benchmark_sample, normalize_turns
    from asr_refinement import build_asr_context
    from atomic_io import atomic_write_json
    from fetcher import render_segments, transcribe
except ImportError:
    from scripts.asr_benchmark import benchmark_sample, normalize_turns
    from scripts.asr_refinement import build_asr_context
    from scripts.atomic_io import atomic_write_json
    from scripts.fetcher import render_segments, transcribe


def resolve_manifest_paths(manifest, manifest_path):
    manifest = json.loads(json.dumps(manifest))
    base = Path(manifest_path).resolve().parent
    for key in ("audio",):
        value = manifest.get(key)
        if value and not Path(value).is_absolute():
            manifest[key] = str((base / value).resolve())
    reference = manifest.setdefault("reference", {})
    for key in ("segments_json", "stm", "rttm", "uem"):
        value = reference.get(key)
        if value and not Path(value).is_absolute():
            reference[key] = str((base / value).resolve())
    return manifest


def file_fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def benchmark_inputs(manifest, manifest_path):
    references = {}
    for key in ("segments_json", "stm", "rttm", "uem"):
        value = manifest.get("reference", {}).get(key)
        if value:
            references[key] = file_fingerprint(value)
    return {
        "manifest": file_fingerprint(manifest_path),
        "audio": file_fingerprint(manifest["audio"]),
        "references": references,
    }


def benchmark_environment():
    packages = {}
    for name in (
        "ctranslate2",
        "faster-whisper",
        "pyannote.audio",
        "pyannote.metrics",
        "torch",
        "whisperx",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
    }


def _quality_value(run, path, default=None):
    value = run
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def recommend_model_policy(
        runs,
        *,
        max_cpwer_regression=0.02,
        max_sawer_regression=0.02,
        min_speedup=1.5,
):
    successful = [
        run for run in runs
        if run.get("status") == "passed"
        and _quality_value(run, ("metrics", "cpwer", "wer")) is not None
        and _quality_value(
            run, ("metrics", "speaker_attributed_wer", "wer")) is not None
    ]
    if not successful:
        return {
            "status": "unavailable",
            "reason": "no_successful_policy_runs",
        }
    best = min(
        successful,
        key=lambda run: (
            _quality_value(run, ("metrics", "cpwer", "wer")),
            _quality_value(
                run, ("metrics", "speaker_attributed_wer", "wer")),
            run["wall_seconds"],
        ),
    )
    best_cpwer = _quality_value(best, ("metrics", "cpwer", "wer"))
    best_sawer = _quality_value(
        best, ("metrics", "speaker_attributed_wer", "wer"))
    candidates = []
    for run in successful:
        cpwer = _quality_value(run, ("metrics", "cpwer", "wer"))
        sawer = _quality_value(
            run, ("metrics", "speaker_attributed_wer", "wer"))
        run_wall = max(float(run["wall_seconds"]), 1e-9)
        speedup = max(
            float(best["wall_seconds"]), 1e-9) / run_wall
        if (
                cpwer <= best_cpwer + max_cpwer_regression
                and sawer <= best_sawer + max_sawer_regression
                and (
                    run["name"] == best["name"]
                    or speedup >= min_speedup
                )):
            candidates.append((run["wall_seconds"], run, speedup))
    first_pass = min(candidates, key=lambda item: item[0])[1]
    speedup = max(
        float(best["wall_seconds"]), 1e-9) / max(
            float(first_pass["wall_seconds"]), 1e-9)
    return {
        "status": "ready",
        "first_pass_policy": first_pass["name"],
        "review_policy": best["name"],
        "first_pass_model": first_pass["model"],
        "review_model": best["model"],
        "speedup_vs_review": round(speedup, 3),
        "cpwer_regression": round(
            _quality_value(first_pass, ("metrics", "cpwer", "wer"))
            - best_cpwer,
            4,
        ),
        "speaker_attributed_wer_regression": round(
            _quality_value(
                first_pass,
                ("metrics", "speaker_attributed_wer", "wer"),
            ) - best_sawer,
            4,
        ),
        "thresholds": {
            "max_cpwer_regression": max_cpwer_regression,
            "max_sawer_regression": max_sawer_regression,
            "min_speedup": min_speedup,
        },
    }


def run_policy(
        manifest,
        policy,
        hypothesis_dir,
        shared_diarization=None):
    audio = manifest["audio"]
    name = policy["name"]
    context = build_asr_context(
        title=manifest.get("title", manifest["id"]),
        hotwords=", ".join(manifest.get("entities", [])),
    )
    started = time.perf_counter()
    result = transcribe(
        audio,
        quality=policy.get("quality", "balanced"),
        asr_model=policy.get("model"),
        language=manifest.get("language", "en"),
        asr_context=context,
        adaptive_refinement=policy.get("adaptive_refinement", True),
        align_audio=policy.get("align", True),
        diarize_audio=(
            policy.get("diarize", True)
            and shared_diarization is None
        ),
        min_speakers=manifest.get("min_speakers"),
        max_speakers=manifest.get("max_speakers"),
        return_metadata=True,
    )
    if shared_diarization is not None:
        from diarize import merge_segments_with_speakers

        result["diarization_turns"] = normalize_turns(
            shared_diarization["turns"])
        result["segments"] = merge_segments_with_speakers(
            result["segments"],
            shared_diarization["turns"],
        )
        result["text"] = render_segments(result["segments"])
        result["meta"]["diarization"] = True
        result["meta"]["diarization_meta"] = shared_diarization["meta"]
        result["meta"]["diarization_model"] = (
            shared_diarization["meta"].get("model"))
        result["meta"]["diarization_exclusive"] = (
            shared_diarization["meta"].get("exclusive_used", False))
        result["meta"]["speaker_count"] = len({
            segment.get("speaker")
            for segment in result["segments"]
            if segment.get("speaker")
        })
    wall_seconds = time.perf_counter() - started
    hypothesis_path = hypothesis_dir / f"{name}.json"
    atomic_write_json(hypothesis_path, result)
    metrics = benchmark_sample(manifest, result)
    return {
        "name": name,
        "model": policy.get("model"),
        "quality": policy.get("quality", "balanced"),
        "status": "passed",
        "wall_seconds": round(wall_seconds, 3),
        "hypothesis": str(hypothesis_path),
        "asr_meta": result.get("meta", {}),
        "metrics": metrics,
    }


def load_shared_diarization(
    path,
    *,
    audio_path,
    min_speakers=None,
    max_speakers=None,
):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected_audio = file_fingerprint(audio_path)
    cached_audio = payload.get("input_audio")
    if not isinstance(cached_audio, dict):
        raise ValueError(
            f"共享 diarization 缓存缺少 input_audio 指纹: {path}")
    for key in ("bytes", "sha256"):
        if cached_audio.get(key) != expected_audio[key]:
            raise ValueError(
                f"共享 diarization 缓存与当前音频不匹配: "
                f"{key} cached={cached_audio.get(key)!r} "
                f"current={expected_audio[key]!r}"
            )
    constraints = payload.get("speaker_constraints", {})
    expected_constraints = {
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
    }
    if constraints != expected_constraints:
        raise ValueError(
            "共享 diarization 缓存的说话人数约束不匹配: "
            f"cached={constraints!r} current={expected_constraints!r}"
        )
    turns = normalize_turns(payload.get("turns", []))
    if not turns:
        raise ValueError(f"共享 diarization 缓存没有 turns: {path}")
    metadata = {
        key: value
        for key, value in payload.items()
        if key not in {"turns", "wall_seconds", "reused"}
    }
    internal_turns = [
        (turn["start"], turn["end"], turn["speaker"])
        for turn in turns
    ]
    return {
        "turns": internal_turns,
        "meta": metadata,
    }, {
        **payload,
        "turns": turns,
        "reused": True,
    }


def _build_report(
    manifest,
    manifest_path,
    shared_diarization_report,
    runs,
):
    policy_config = manifest.get("policy_selection", {})
    return {
        "schema_version": 2,
        "sample_id": manifest["id"],
        "manifest": str(Path(manifest_path).resolve()),
        "inputs": benchmark_inputs(manifest, manifest_path),
        "environment": benchmark_environment(),
        "shared_diarization": shared_diarization_report,
        "runs": runs,
        "recommendation": recommend_model_policy(
            runs,
            max_cpwer_regression=float(
                policy_config.get("max_cpwer_regression", 0.02)),
            max_sawer_regression=float(
                policy_config.get(
                    "max_speaker_attributed_wer_regression", 0.02)),
            min_speedup=float(
                policy_config.get("min_speedup", 1.5)),
        ),
    }


def run_manifest(
    manifest_path,
    output_dir=None,
    reuse_shared_diarization=False,
):
    manifest_path = Path(manifest_path)
    manifest = resolve_manifest_paths(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        manifest_path,
    )
    output_dir = Path(
        output_dir
        or manifest.get("output_dir")
        or manifest_path.parent / "results" / manifest["id"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    hypothesis_dir = output_dir / "hypotheses"
    hypothesis_dir.mkdir(parents=True, exist_ok=True)

    shared_diarization = None
    shared_diarization_report = None
    if manifest.get("shared_diarization"):
        shared_path = output_dir / "shared_diarization.json"
        if reuse_shared_diarization:
            if not shared_path.is_file():
                raise FileNotFoundError(
                    f"共享 diarization 缓存不存在: {shared_path}")
            (
                shared_diarization,
                shared_diarization_report,
            ) = load_shared_diarization(
                shared_path,
                audio_path=manifest["audio"],
                min_speakers=manifest.get("min_speakers"),
                max_speakers=manifest.get("max_speakers"),
            )
        else:
            from diarize import diarize

            started = time.perf_counter()
            shared_diarization = diarize(
                manifest["audio"],
                min_speakers=manifest.get("min_speakers"),
                max_speakers=manifest.get("max_speakers"),
                return_metadata=True,
            )
            shared_diarization_report = {
                **shared_diarization["meta"],
                "wall_seconds": round(
                    time.perf_counter() - started, 3),
                "input_audio": file_fingerprint(manifest["audio"]),
                "speaker_constraints": {
                    "min_speakers": manifest.get("min_speakers"),
                    "max_speakers": manifest.get("max_speakers"),
                },
                "turns": normalize_turns(shared_diarization["turns"]),
                "reused": False,
            }
            atomic_write_json(
                shared_path,
                shared_diarization_report,
            )

    runs = []
    for policy in manifest.get("policies", []):
        try:
            runs.append(run_policy(
                manifest,
                policy,
                hypothesis_dir,
                shared_diarization=shared_diarization,
            ))
        except Exception as exc:
            runs.append({
                "name": policy["name"],
                "model": policy.get("model"),
                "quality": policy.get("quality", "balanced"),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
    report = _build_report(
        manifest,
        manifest_path,
        shared_diarization_report,
        runs,
    )
    atomic_write_json(output_dir / "report.json", report)
    return report


def rescore_manifest(manifest_path, output_dir=None):
    manifest_path = Path(manifest_path)
    manifest = resolve_manifest_paths(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        manifest_path,
    )
    output_dir = Path(
        output_dir
        or manifest.get("output_dir")
        or manifest_path.parent / "results" / manifest["id"]
    )
    previous_path = output_dir / "report.json"
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8"))
        if previous_path.is_file() else {}
    )
    previous_runs = {
        run.get("name"): run
        for run in previous.get("runs", [])
        if run.get("name")
    }
    shared_path = output_dir / "shared_diarization.json"
    shared_report = previous.get("shared_diarization")
    if not isinstance(shared_report, dict) and shared_path.is_file():
        shared_report = json.loads(
            shared_path.read_text(encoding="utf-8"))
    if isinstance(shared_report, dict):
        shared_report = {
            **shared_report,
            "reused": True,
            "rescore_only": True,
        }
    runs = []
    for policy in manifest.get("policies", []):
        name = policy["name"]
        hypothesis_path = output_dir / "hypotheses" / f"{name}.json"
        if not hypothesis_path.is_file():
            runs.append({
                "name": name,
                "model": policy.get("model"),
                "quality": policy.get("quality", "balanced"),
                "status": "failed",
                "error": f"FileNotFoundError: {hypothesis_path}",
            })
            continue
        payload = json.loads(
            hypothesis_path.read_text(encoding="utf-8"))
        previous_run = previous_runs.get(name, {})
        wall_seconds = previous_run.get("wall_seconds")
        if not isinstance(wall_seconds, (int, float)):
            runs.append({
                "name": name,
                "model": policy.get("model"),
                "quality": policy.get("quality", "balanced"),
                "status": "failed",
                "error": "previous report missing wall_seconds",
            })
            continue
        runs.append({
            "name": name,
            "model": policy.get("model"),
            "quality": policy.get("quality", "balanced"),
            "status": "passed",
            "wall_seconds": wall_seconds,
            "hypothesis": str(hypothesis_path),
            "asr_meta": payload.get("meta", {}),
            "metrics": benchmark_sample(manifest, payload),
        })
    report = _build_report(
        manifest,
        manifest_path,
        shared_report,
        runs,
    )
    atomic_write_json(previous_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="运行多说话人 ASR benchmark policies")
    parser.add_argument("manifest")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--reuse-shared-diarization",
        action="store_true",
        help="复用 output-dir 中已有的 shared_diarization.json",
    )
    parser.add_argument(
        "--rescore-only",
        action="store_true",
        help="复用已有 hypotheses，仅重新计算指标和 recommendation",
    )
    args = parser.parse_args()
    if args.rescore_only:
        report = rescore_manifest(
            args.manifest,
            output_dir=args.output_dir,
        )
    else:
        report = run_manifest(
            args.manifest,
            output_dir=args.output_dir,
            reuse_shared_diarization=args.reuse_shared_diarization,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(
        run.get("status") == "passed" for run in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

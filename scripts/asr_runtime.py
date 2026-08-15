"""ASR runtime diagnostics, CUDA preparation, and repeatable benchmarks."""
from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

try:
    from hashing import sha256_text as _text_sha256
except ImportError:
    from scripts.hashing import sha256_text as _text_sha256

try:
    from atomic_io import atomic_write_json
except ImportError:
    from scripts.atomic_io import atomic_write_json


_PRELOADED_CUDA_LIBRARIES = []
_CUDA_LIBRARY_PATTERNS = (
    "libcublasLt.so.12",
    "libcublas.so.12",
    "libcudnn.so.9",
)


@dataclass(frozen=True)
class RuntimeSpec:
    device: str
    compute_type: str

    @classmethod
    def parse(cls, value):
        device, separator, compute_type = value.partition(":")
        if not separator or not device or not compute_type:
            raise ValueError(
                f"运行配置格式应为 device:compute_type，当前值: {value!r}")
        if device not in {"cpu", "cuda"}:
            raise ValueError(f"不支持的 ASR device: {device}")
        return cls(device=device, compute_type=compute_type)


@lru_cache(maxsize=16)
def resolve_runtime(device="auto", compute_type="auto"):
    """Resolve an explicit or automatic runtime without hiding explicit errors."""
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"不支持的 ASR device: {device}")
    if device == "auto":
        report = inspect_runtime(preload_cuda=False)
        device = "cuda" if report.get("cuda_ready") else "cpu"
    if compute_type == "auto":
        compute_type = "int8_float16" if device == "cuda" else "int8"

    try:
        import ctranslate2
        supported = ctranslate2.get_supported_compute_types(device)
    except Exception as exc:
        raise RuntimeError(
            f"无法检查 {device} ASR compute type: {exc}") from exc
    if compute_type not in supported:
        raise RuntimeError(
            f"{device} 不支持 ASR_COMPUTE_TYPE={compute_type}；"
            f"可选值: {', '.join(sorted(supported))}"
        )
    if device == "cuda":
        prepare_cuda_runtime()
    return RuntimeSpec(device=device, compute_type=compute_type)


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run_text(command, timeout=10):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    return result.stdout.strip(), None


def _site_package_roots():
    roots = []
    for entry in sys.path:
        path = Path(entry)
        if path.is_dir() and path.name in {"site-packages", "dist-packages"}:
            roots.append(path)
    return tuple(dict.fromkeys(roots))


def cuda_library_dirs():
    """Return installed NVIDIA wheel library directories."""
    matches = []
    for root in _site_package_roots():
        nvidia = root / "nvidia"
        if not nvidia.is_dir():
            continue
        for path in sorted(nvidia.glob("*/lib")):
            if path.is_dir():
                matches.append(path.resolve())
    return tuple(dict.fromkeys(matches))


def _find_cuda_library(name):
    for directory in cuda_library_dirs():
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def prepare_cuda_runtime():
    """Preload CUDA wheel libraries so CTranslate2 can resolve them reliably."""
    global _PRELOADED_CUDA_LIBRARIES
    if _PRELOADED_CUDA_LIBRARIES:
        return {
            "loaded": [str(path) for path in _PRELOADED_CUDA_LIBRARIES],
            "library_dirs": [str(path) for path in cuda_library_dirs()],
        }

    missing = []
    resolved = []
    for name in _CUDA_LIBRARY_PATTERNS:
        path = _find_cuda_library(name)
        if path is None:
            missing.append(name)
        else:
            resolved.append(path)
    if missing:
        raise RuntimeError(
            "CUDA ASR 运行库不完整，缺少 "
            f"{', '.join(missing)}；请安装 requirements-asr-gpu.txt"
        )

    loaded = []
    for path in resolved:
        try:
            ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            raise RuntimeError(
                f"无法加载 CUDA ASR 运行库 {path}: {exc}") from exc
        loaded.append(path)
    _PRELOADED_CUDA_LIBRARIES = loaded
    return {
        "loaded": [str(path) for path in loaded],
        "library_dirs": [str(path) for path in cuda_library_dirs()],
    }


def _nvidia_report():
    output, error = _run_text([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ])
    if error or not output:
        return {"available": False, "error": error}
    devices = []
    for line in output.splitlines():
        values = [part.strip() for part in line.split(",")]
        if len(values) != 5:
            continue
        devices.append({
            "name": values[0],
            "memory_total_mb": int(values[1]),
            "memory_free_mb": int(values[2]),
            "driver_version": values[3],
            "compute_capability": values[4],
        })
    return {"available": bool(devices), "devices": devices}


def inspect_runtime(preload_cuda=False):
    """Return deterministic ASR runtime capabilities and readiness."""
    report = {
        "python": sys.version.split()[0],
        "packages": {
            name: _package_version(name)
            for name in (
                "faster-whisper",
                "ctranslate2",
                "nvidia-cublas-cu12",
                "nvidia-cudnn-cu12",
                "torch",
                "torchaudio",
            )
        },
        "nvidia": _nvidia_report(),
        "cuda_library_dirs": [
            str(path) for path in cuda_library_dirs()
        ],
        "required_cuda_libraries": {
            name: (
                str(path) if (path := _find_cuda_library(name)) else None
            )
            for name in _CUDA_LIBRARY_PATTERNS
        },
    }
    try:
        import ctranslate2

        report["ctranslate2"] = {
            "cuda_device_count": ctranslate2.get_cuda_device_count(),
            "cpu_compute_types": sorted(
                ctranslate2.get_supported_compute_types("cpu")),
            "cuda_compute_types": sorted(
                ctranslate2.get_supported_compute_types("cuda")),
        }
    except Exception as exc:
        report["ctranslate2"] = {
            "error": f"{type(exc).__name__}: {exc}",
        }

    libraries_present = all(
        report["required_cuda_libraries"].values())
    cuda_devices = report["ctranslate2"].get("cuda_device_count", 0)
    report["cuda_ready"] = bool(libraries_present and cuda_devices)
    if preload_cuda:
        try:
            report["cuda_preload"] = prepare_cuda_runtime()
        except RuntimeError as exc:
            report["cuda_preload"] = {"error": str(exc)}
            report["cuda_ready"] = False
    try:
        resolved = resolve_runtime_from_report(report)
        report["automatic_runtime"] = asdict(resolved)
    except RuntimeError as exc:
        report["automatic_runtime"] = {"error": str(exc)}
    return report


def resolve_runtime_from_report(report):
    """Resolve the automatic runtime from a precomputed doctor report."""
    device = "cuda" if report.get("cuda_ready") else "cpu"
    compute_type = "int8_float16" if device == "cuda" else "int8"
    supported = report.get("ctranslate2", {}).get(
        f"{device}_compute_types", [])
    if compute_type not in supported:
        raise RuntimeError(
            f"{device} 不支持自动 compute type {compute_type}")
    return RuntimeSpec(device=device, compute_type=compute_type)


def _audio_duration_seconds(path):
    output, error = _run_text([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=30)
    if error or not output:
        return None
    try:
        return float(output)
    except ValueError:
        return None



class _GpuMemorySampler:
    def __init__(self, interval=0.2):
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _memory_used_mb():
        output, error = _run_text([
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ], timeout=5)
        if error or not output:
            return None
        try:
            return max(int(line.strip()) for line in output.splitlines())
        except ValueError:
            return None

    def __enter__(self):
        baseline = self._memory_used_mb()
        if baseline is not None:
            self.samples.append(baseline)

        def sample():
            while not self._stop.wait(self.interval):
                value = self._memory_used_mb()
                if value is not None:
                    self.samples.append(value)

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 3))
        value = self._memory_used_mb()
        if value is not None:
            self.samples.append(value)

    def metrics(self):
        if not self.samples:
            return {}
        baseline = self.samples[0]
        peak = max(self.samples)
        return {
            "gpu_memory_baseline_mb": baseline,
            "gpu_memory_peak_mb": peak,
            "gpu_memory_peak_delta_mb": max(0, peak - baseline),
        }


def benchmark_transcription(
        audio_path,
        model_size,
        runtime,
        *,
        language="en",
        beam_size=5,
        model_factory=None,
):
    """Benchmark one model/runtime pair and return machine-readable metrics."""
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)
    if not isinstance(runtime, RuntimeSpec):
        runtime = RuntimeSpec.parse(str(runtime))
    if runtime.device == "cuda":
        prepare_cuda_runtime()

    if model_factory is None:
        from faster_whisper import WhisperModel
        model_factory = WhisperModel

    sampler = _GpuMemorySampler() if runtime.device == "cuda" else None
    context = sampler if sampler is not None else _NullContext()
    with context:
        load_started = time.perf_counter()
        model = model_factory(
            model_size,
            device=runtime.device,
            compute_type=runtime.compute_type,
        )
        model_load_seconds = time.perf_counter() - load_started
        transcribe_started = time.perf_counter()
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            word_timestamps=False,
            **({"language": language} if language else {}),
        )
        materialized = list(segments)
        transcription_seconds = time.perf_counter() - transcribe_started

    text = " ".join(
        (getattr(segment, "text", "") or "").strip()
        for segment in materialized
        if (getattr(segment, "text", "") or "").strip()
    ).strip()
    duration = _audio_duration_seconds(audio_path)
    result = {
        "model": model_size,
        **asdict(runtime),
        "beam_size": beam_size,
        "requested_language": language or "auto",
        "detected_language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "audio_file": str(audio_path),
        "audio_duration_seconds": (
            round(duration, 3) if duration is not None else None),
        "model_load_seconds": round(model_load_seconds, 3),
        "transcription_seconds": round(transcription_seconds, 3),
        "total_seconds": round(
            model_load_seconds + transcription_seconds, 3),
        "realtime_factor": (
            round(transcription_seconds / duration, 4)
            if duration else None
        ),
        "segment_count": len(materialized),
        "transcript_chars": len(text),
        "transcript_sha256": _text_sha256(text),
        "transcript_text": text,
        "transcript_preview": text[:300],
        "process_peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            1,
        ),
    }
    if sampler is not None:
        result.update(sampler.metrics())
    return result


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


def _make_clip(source, start, duration, output):
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{duration:.3f}",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(output),
        ],
        capture_output=True,
        check=True,
        timeout=max(60, min(900, round(duration * 8 + 60))),
    )


def main():
    parser = argparse.ArgumentParser(
        description="ASR runtime 诊断与 CPU/GPU 基准")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查 CUDA、CTranslate2 和运行库")
    doctor.add_argument(
        "--preload-cuda", action="store_true",
        help="实际预加载 cuBLAS/cuDNN，验证动态链接",
    )
    doctor.add_argument(
        "--require-cuda", action="store_true",
        help="CUDA 未就绪时返回非零状态",
    )

    benchmark = sub.add_parser(
        "benchmark", help="在短音频上比较一个或多个运行配置")
    benchmark.add_argument("audio")
    benchmark.add_argument("--model", default="tiny.en")
    benchmark.add_argument(
        "--runtime",
        action="append",
        default=[],
        help="可重复；格式 cpu:int8 或 cuda:int8_float16",
    )
    benchmark.add_argument("--language", default="en")
    benchmark.add_argument("--beam-size", type=int, default=5)
    benchmark.add_argument("--start", type=float, default=0.0)
    benchmark.add_argument("--duration", type=float, default=30.0)
    benchmark.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.command == "doctor":
        result = inspect_runtime(preload_cuda=args.preload_cuda)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.require_cuda and not result["cuda_ready"] else 0

    runtimes = [
        RuntimeSpec.parse(value)
        for value in (
            args.runtime or ["cpu:int8", "cuda:int8_float16"]
        )
    ]
    source = Path(args.audio)
    if not source.is_file():
        raise FileNotFoundError(source)
    with tempfile.TemporaryDirectory(prefix="asr-benchmark-") as temp_dir:
        clip = Path(temp_dir) / "clip.wav"
        _make_clip(source, args.start, args.duration, clip)
        result = {
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_audio": str(source.resolve()),
            "clip_start_seconds": args.start,
            "clip_duration_seconds": args.duration,
            "model": args.model,
            "runs": [],
        }
        for runtime in runtimes:
            run = benchmark_transcription(
                    clip,
                    args.model,
                    runtime,
                    language=None if args.language == "auto" else args.language,
                    beam_size=args.beam_size,
                )
            run["audio_file"] = str(source.resolve())
            run["clip_start_seconds"] = args.start
            run["clip_duration_seconds"] = args.duration
            result["runs"].append(run)
    if args.output:
        atomic_write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
